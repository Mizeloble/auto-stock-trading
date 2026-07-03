"""진입 축 3종 반사실 검증 — 2026-07-04 적대적 검토 후속 (read-only).

무엇/왜: 적대적 검토가 지목한 세 가지 진입 축을 실데이터로 검증한다.
  A. 재진입 쿨다운 — 레짐 프로파일(45~60분) vs current(90분, 2026-05-16 사람 결정).
     레짐 라우팅이 켜지며 사람 결정(90)이 실전에 적용되지 않던 암묵 변경의 손익 영향.
  B. 개장 직후 진입 — 실제 진입이 09:09~09:10(모멘텀 lookback 이 처음 채워지는 분)에
     몰리는데, 그 시각은 하루 중 변동 최대 구간. 진입을 뒤로 미뤘다면(09:20~11:00)
     같은 종목·같은 가드 규칙으로 손익이 어땠나를 분봉으로 시뮬레이션.
  C. 진입 가격대 분해 — 발굴이 거래대금이 아닌 거래량(주식 수) 순위(ka10030
     sort_tp=1)라 저가주가 상위 독식. 가격대별 성과·틱 크기 대비 트레일링 폭 실측.

데이터:
- 원장: logs/paper-day-*.log 를 reports/log_parser.parse_log 로 재구성
  (analyze_trades.py 와 동일 페어링·비용 원칙 — 중복 파서 금지, FIFO 매칭).
- 반사실 가격: ka10080 분봉(수정주가). 시뮬레이터는 PositionGuard 와 같은
  분봉 종가 기준 stop→trailing→partial 우선순위 + 15:15 청산을 재현.
- 그날 가드 파라미터: 리포트의 regime → REGIME_PROFILE_MAP → 프로파일 YAML.

한계(정직 고지):
- 표본이 작다(로그가 남은 2026-06 이후 거래일 4일, 왕복 15건). 방향 참고용이지
  단독 결론 근거가 아니다.
- 반사실 진입은 "그 시각에 모멘텀 게이트가 통과했다"고 가정 — 게이트가 막았을
  수도 있으므로 지연 진입 쪽 거래 수를 과대평가할 수 있다.
- 시뮬 체결가 = 분봉 종가 (dry-run 과 동일 근사). 실슬리피지는 비용 상수로만 반영.

실행: uv run python scripts/diagnostics/entry_axes_study.py
"""

from __future__ import annotations

import asyncio
import contextlib
import glob
import itertools
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from auto_trading._common import KST, to_decimal
from auto_trading.daytrade.profile import DaytradeProfile
from auto_trading.kiwoom.endpoints import PATH_CHART, TR
from auto_trading.kiwoom.rest import KiwoomRestClient
from auto_trading.reports.daily import (
    COST_COMMISSION_RATE,
    COST_SLIPPAGE_BPS,
    COST_TAX_RATE_SELL,
)
from auto_trading.reports.log_parser import parse_log
from auto_trading.strategy.regime_router import REGIME_PROFILE_MAP
from auto_trading.strategy.scanner import MarketRegime

SLIP_RATE = COST_SLIPPAGE_BPS / 10_000.0
LOG_GLOB = "logs/paper-day-*.log"
REPORT_DIR = Path("data/reports")
PROFILES_ROOT = Path("strategies/profiles")
LIQ_TIME = time(15, 15)
DELAY_GRID = [time(9, 20), time(9, 30), time(10, 0), time(10, 30), time(11, 0)]
COOLDOWN_HUMAN_MIN = 90  # 2026-05-16 사람 결정 (daytrade-current.yaml)


# ---------------------------------------------------------------- 원장 재구성


@dataclass
class Trip:
    day: date
    symbol: str
    entry_ts: datetime  # KST
    exit_ts: datetime  # KST
    qty: float
    buy_price: float
    sell_price: float

    @property
    def gross_pnl(self) -> float:
        return (self.sell_price - self.buy_price) * self.qty

    @property
    def cost(self) -> float:
        return _round_trip_cost(self.buy_price, self.sell_price, self.qty)

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.cost


def _round_trip_cost(buy_price: float, sell_price: float, qty: float) -> float:
    buy_leg = buy_price * qty * (COST_COMMISSION_RATE + SLIP_RATE)
    sell_leg = sell_price * qty * (COST_COMMISSION_RATE + COST_TAX_RATE_SELL + SLIP_RATE)
    return buy_leg + sell_leg


def _ts_kst(record: dict) -> datetime | None:
    raw = record.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(KST)
    except ValueError:
        return None


def load_trips() -> list[Trip]:
    """로그 → FIFO 라운드트립. analyze_trades.py 와 같은 원칙(양쪽 가격 있는 분량만)."""
    trips: list[Trip] = []
    for path_str in sorted(glob.glob(LOG_GLOB)):
        path = Path(path_str)
        day = date.fromisoformat(path.name.replace("paper-day-", "").replace(".log", ""))
        events = parse_log(path)
        fills: list[tuple[datetime, str, str, float, float | None]] = []
        for rec in (*events.orders_accepted, *events.orders_simulated):
            side = str(rec.get("side", "")).lower()
            ts = _ts_kst(rec)
            if side not in ("buy", "sell") or ts is None:
                continue
            price = rec.get("price")
            fills.append(
                (
                    ts,
                    str(rec.get("symbol", "")),
                    side,
                    float(rec.get("qty", rec.get("quantity", 0)) or 0),
                    float(price) if isinstance(price, (int, float)) else None,
                )
            )
        for rec in (*events.liquidations_accepted, *events.liquidations_simulated):
            ts = _ts_kst(rec)
            if ts is None:
                continue
            price = rec.get("sell_price", rec.get("price"))
            fills.append(
                (
                    ts,
                    str(rec.get("symbol", "")),
                    "sell",
                    float(rec.get("qty", rec.get("quantity", 0)) or 0),
                    float(price) if isinstance(price, (int, float)) else None,
                )
            )
        fills.sort(key=lambda f: f[0])

        queues: dict[str, list[list]] = {}
        for ts, symbol, side, qty, price in fills:
            if side == "buy":
                if qty > 0:
                    queues.setdefault(symbol, []).append([qty, price, ts])
                continue
            remaining = qty
            q = queues.get(symbol) or []
            while remaining > 0 and q:
                lot = q[0]
                take = min(remaining, lot[0])
                if lot[1] is not None and price is not None:
                    trips.append(
                        Trip(
                            day=day,
                            symbol=symbol,
                            entry_ts=lot[2],
                            exit_ts=ts,
                            qty=take,
                            buy_price=lot[1],
                            sell_price=price,
                        )
                    )
                remaining -= take
                lot[0] -= take
                if lot[0] <= 0:
                    q.pop(0)
    return trips


def _merge_partial_exits(trips: list[Trip]) -> list[Trip]:
    """부분익절로 쪼개진 트립을 (종목·진입시각) 단위로 합쳐 1진입=1왕복로 본다."""
    merged: dict[tuple[date, str, datetime], Trip] = {}
    for t in trips:
        key = (t.day, t.symbol, t.entry_ts)
        prev = merged.get(key)
        if prev is None:
            merged[key] = Trip(**vars(t))
            continue
        total_qty = prev.qty + t.qty
        prev.sell_price = (prev.sell_price * prev.qty + t.sell_price * t.qty) / total_qty
        prev.qty = total_qty
        prev.exit_ts = max(prev.exit_ts, t.exit_ts)
    return sorted(merged.values(), key=lambda t: (t.day, t.entry_ts))


# ------------------------------------------------- 그날 가드 파라미터 (레짐 기반)


@dataclass(frozen=True)
class GuardParams:
    stop: float
    trail: float
    partial: float
    partial_ratio: float


def guard_params_for(day: date) -> GuardParams:
    """리포트의 regime → 레짐 프로파일 YAML 의 가드 파라미터. 없으면 highvol 폴백.

    한계: 프로파일 YAML 의 '현재' 값을 쓴다 — 해당일 이후 YAML 이 바뀌었다면 어긋날
    수 있다(2026-06 이후 가드 파라미터 변경 이력 없음을 git log 로 확인하고 사용).
    """
    regime_name = "high_vol"
    report = REPORT_DIR / f"daily-{day.isoformat()}.json"
    if report.exists():
        with contextlib.suppress(OSError, json.JSONDecodeError):
            regime_name = json.loads(report.read_text()).get("regime") or regime_name
    profile_stem = REGIME_PROFILE_MAP.get(MarketRegime(regime_name), "daytrade-highvol")
    profile = DaytradeProfile.load_yaml(PROFILES_ROOT / f"{profile_stem}.yaml")
    return GuardParams(
        stop=profile.risk.stop_loss_pct,
        trail=profile.risk.trailing_stop_pct,
        partial=profile.risk.partial_take_pct,
        partial_ratio=profile.risk.partial_take_qty_ratio,
    )


# ---------------------------------------------------------------- 분봉 fetch


async def fetch_minute_closes(
    client: KiwoomRestClient, symbol: str, *, oldest_day: date
) -> dict[date, list[tuple[datetime, float]]]:
    """ka10080 1분봉 → 일자별 (ts, close) 오름차순. oldest_day 까지 페이지네이션."""
    body = {"stk_cd": symbol, "tic_scope": "1", "upd_stkpc_tp": "1"}
    by_day: dict[date, list[tuple[datetime, float]]] = {}
    async for page in client.paginate(TR.CHART_MINUTE, PATH_CHART, body, max_pages=30):
        rows = None
        for v in page.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                rows = v
                break
        if not rows:
            continue
        reached_past = False
        for row in rows:
            raw_tm = str(row.get("cntr_tm") or "")
            raw_px = row.get("cur_prc")
            if len(raw_tm) != 14 or raw_px in (None, ""):
                continue
            ts = datetime.strptime(raw_tm, "%Y%m%d%H%M%S").replace(tzinfo=KST)
            if ts.date() < oldest_day:
                reached_past = True
                continue
            by_day.setdefault(ts.date(), []).append((ts, float(abs(to_decimal(raw_px)))))
        if reached_past:
            break
    for series in by_day.values():
        series.sort(key=lambda p: p[0])
    return by_day


# ---------------------------------------------------------------- 시뮬레이터


@dataclass(frozen=True)
class SimResult:
    entry_ts: datetime
    entry_price: float
    exit_reason: str  # stop / trailing / liquidate / partial+trailing / partial+liquidate
    net_return_pct: float  # 비용 차감, 투입 notional 대비 %
    hold_min: float


def simulate_entry(
    day_bars: list[tuple[datetime, float]],
    *,
    entry_at: time,
    params: GuardParams,
    qty: float = 1.0,
) -> SimResult | None:
    """PositionGuard 의 분봉 종가 판정(stop→trailing→partial 우선순위)을 재현.

    entry_at 이후 첫 봉 종가로 매수 → 매 봉 종가에서 가드 판정 → 15:15 잔량 청산.
    반환 net_return_pct 는 투입 notional 대비 순수익률(%) — 수량 무관 비교용.
    """
    bars = [(ts, px) for ts, px in day_bars if ts.time() >= entry_at]
    if not bars:
        return None
    entry_ts, entry_price = bars[0]
    if entry_price <= 0 or entry_ts.time() >= LIQ_TIME:
        return None

    highest = entry_price
    remaining = qty
    partial_done = False
    legs: list[tuple[float, float]] = []  # (sell_price, qty)
    exit_reason = "liquidate"
    exit_ts = entry_ts

    for ts, px in bars[1:]:
        if ts.time() >= LIQ_TIME:
            exit_ts = ts
            break
        highest = max(highest, px)
        if params.stop > 0 and 1.0 - px / entry_price >= params.stop:
            legs.append((px, remaining))
            remaining = 0.0
            exit_reason = "partial+stop" if partial_done else "stop"
            exit_ts = ts
            break
        if params.trail > 0 and 1.0 - px / highest >= params.trail:
            legs.append((px, remaining))
            remaining = 0.0
            exit_reason = "partial+trailing" if partial_done else "trailing"
            exit_ts = ts
            break
        if params.partial > 0 and not partial_done and px / entry_price - 1.0 >= params.partial:
            sell_qty = remaining * params.partial_ratio
            legs.append((px, sell_qty))
            remaining -= sell_qty
            partial_done = True
    if remaining > 0:
        liq_bars = [(ts, px) for ts, px in day_bars if ts.time() >= LIQ_TIME]
        liq_ts, liq_px = liq_bars[0] if liq_bars else day_bars[-1]
        legs.append((liq_px, remaining))
        exit_reason = "partial+liquidate" if partial_done else exit_reason
        exit_ts = liq_ts

    gross = sum((sell_px - entry_price) * leg_qty for sell_px, leg_qty in legs)
    cost = sum(_round_trip_cost(entry_price, sell_px, leg_qty) for sell_px, leg_qty in legs)
    notional = entry_price * qty
    return SimResult(
        entry_ts=entry_ts,
        entry_price=entry_price,
        exit_reason=exit_reason,
        net_return_pct=(gross - cost) / notional * 100.0,
        hold_min=(exit_ts - entry_ts).total_seconds() / 60.0,
    )


# ---------------------------------------------------------------- 섹션 A/B/C


def krx_tick(price: float) -> float:
    """KRX 호가단위 (2023-01 개편 기준, KOSPI/KOSDAQ 공통)."""
    for limit, tick in (
        (2_000, 1),
        (5_000, 5),
        (20_000, 10),
        (50_000, 50),
        (200_000, 100),
        (500_000, 500),
    ):
        if price < limit:
            return float(tick)
    return 1_000.0


def section_a_cooldown(
    trips: list[Trip], bars: dict[tuple[str, date], list[tuple[datetime, float]]]
) -> None:
    print("=" * 72)
    print("A. 재진입 쿨다운 — 레짐 프로파일(45~60분) vs 사람 결정(90분)")
    print("=" * 72)
    by_symbol_day: dict[tuple[date, str], list[Trip]] = {}
    for t in trips:
        by_symbol_day.setdefault((t.day, t.symbol), []).append(t)

    reentries: list[tuple[Trip, float]] = []  # (트립, 직전 청산 후 경과 분)
    for group in by_symbol_day.values():
        group.sort(key=lambda t: t.entry_ts)
        for prev, cur in itertools.pairwise(group):
            gap_min = (cur.entry_ts - prev.exit_ts).total_seconds() / 60.0
            reentries.append((cur, gap_min))

    if not reentries:
        print("재진입 트립 없음 — 쿨다운 축은 표본 0.")
        return

    blocked = [(t, g) for t, g in reentries if g < COOLDOWN_HUMAN_MIN]
    print(
        f"재진입 트립 {len(reentries)}건 / 그중 90분 미만(=90이면 그 시각 진입 불가) {len(blocked)}건"
    )
    for t, g in reentries:
        mark = "◀ 90분이면 차단" if g < COOLDOWN_HUMAN_MIN else ""
        print(
            f"  {t.day} {t.symbol} 재진입 {t.entry_ts:%H:%M} (직전 청산 후 {g:.0f}분)"
            f" 순손익 {t.net_pnl:+,.0f}원 {mark}"
        )
    removal = sum(t.net_pnl for t, _ in blocked)
    print(
        f"[반사실 1 — 단순 제거] 90분 쿨다운이었다면 사라졌을 재진입 순손익 합: {removal:+,.0f}원"
    )
    print("  (양수면 90분이 이익을 깎음, 음수면 90분이 손실을 막음)")

    print("[반사실 2 — 지연 진입] 같은 종목을 '직전 청산+90분' 봉에서 재진입했다면:")
    shifted_total = 0.0
    actual_total = 0.0
    for t, g in blocked:
        day_bars = bars.get((t.symbol, t.day))
        if not day_bars:
            print(f"  {t.day} {t.symbol}: 분봉 없음 — 스킵")
            continue
        prev_exit = t.entry_ts - timedelta(minutes=g)
        shift_at = (prev_exit + timedelta(minutes=COOLDOWN_HUMAN_MIN)).time()
        params = guard_params_for(t.day)
        sim = simulate_entry(day_bars, entry_at=shift_at, params=params)
        actual_total += t.net_pnl
        if sim is None:
            print(
                f"  {t.day} {t.symbol}: {shift_at:%H:%M} 이후 진입 불가(청산 임박) → 거래 소멸, 실제 {t.net_pnl:+,.0f}원 회피"
            )
            continue
        shifted_won = sim.net_return_pct / 100.0 * t.buy_price * t.qty
        shifted_total += shifted_won
        print(
            f"  {t.day} {t.symbol}: 실제({t.entry_ts:%H:%M}) {t.net_pnl:+,.0f}원 → "
            f"90분 쿨다운({sim.entry_ts:%H:%M} 진입, {sim.exit_reason}) {shifted_won:+,.0f}원"
        )
    print(
        f"  차단 대상 합계: 실제 {actual_total:+,.0f}원 → 지연 시 {shifted_total:+,.0f}원 (Δ {shifted_total - actual_total:+,.0f}원)"
    )


def section_b_entry_delay(
    trips: list[Trip], bars: dict[tuple[str, date], list[tuple[datetime, float]]]
) -> None:
    print()
    print("=" * 72)
    print("B. 개장 직후 진입 vs 지연 진입 — 첫 진입만, 같은 가드 규칙으로 재시뮬")
    print("=" * 72)
    first_entries: dict[tuple[date, str], Trip] = {}
    for t in trips:
        key = (t.day, t.symbol)
        if key not in first_entries or t.entry_ts < first_entries[key].entry_ts:
            first_entries[key] = t

    grid_totals: dict[str, list[float]] = {
        label: [] for label in ["실제(재현)", *[f"{d:%H:%M}" for d in DELAY_GRID]]
    }
    print(
        f"{'일자':<11}{'종목':<8}{'실진입':<7}{'실제순손익':>12} | 재현 + 지연 그리드 (순수익률 %)"
    )
    for (day, symbol), t in sorted(first_entries.items()):
        day_bars = bars.get((symbol, day))
        if not day_bars:
            print(f"{day!s:<11}{symbol:<8} 분봉 없음 — 스킵")
            continue
        params = guard_params_for(day)
        cells: list[str] = []
        base = simulate_entry(day_bars, entry_at=t.entry_ts.time(), params=params)
        if base is not None:
            grid_totals["실제(재현)"].append(base.net_return_pct)
            cells.append(f"재현 {base.net_return_pct:+.2f}%")
        for d in DELAY_GRID:
            sim = simulate_entry(day_bars, entry_at=d, params=params)
            if sim is None:
                cells.append(f"{d:%H:%M} —")
                continue
            grid_totals[f"{d:%H:%M}"].append(sim.net_return_pct)
            cells.append(f"{d:%H:%M} {sim.net_return_pct:+.2f}%")
        actual_pct = t.net_pnl / (t.buy_price * t.qty) * 100.0
        print(
            f"{day!s:<11}{symbol:<8}{t.entry_ts:%H:%M}  {t.net_pnl:>+10,.0f}원"
            f" ({actual_pct:+.2f}%) | " + "  ".join(cells)
        )

    print("-" * 72)
    print("진입 시각별 평균 순수익률 (첫 진입, 동일 종목·동일 규칙):")
    for label, vals in grid_totals.items():
        if not vals:
            continue
        mean = sum(vals) / len(vals)
        wins = sum(1 for v in vals if v > 0)
        print(f"  {label:<10} 평균 {mean:+.2f}%  (승 {wins}/{len(vals)})")
    print("  주의: 지연 진입은 '그 시각 모멘텀 게이트 통과' 가정 — 거래 수 과대평가 가능.")


def section_c_price_band(trips: list[Trip]) -> None:
    print()
    print("=" * 72)
    print("C. 진입 가격대 분해 — 거래량(주수) 순위 발굴의 저가주 편향")
    print("=" * 72)
    bands = [
        ("5천원 미만", 0, 5_000),
        ("5천~2만원", 5_000, 20_000),
        ("2만~10만원", 20_000, 100_000),
        ("10만원 이상", 100_000, float("inf")),
    ]
    print(
        f"{'가격대':<10}{'왕복':>4}{'승':>3}{'평균순손익':>12}{'순손익합':>12}{'틱/트레일링':>12}"
    )
    for label, lo, hi in bands:
        group = [t for t in trips if lo <= t.buy_price < hi]
        if not group:
            print(f"{label:<10}{0:>4}")
            continue
        wins = sum(1 for t in group if t.net_pnl > 0)
        total = sum(t.net_pnl for t in group)
        # 틱 크기가 트레일링 폭(0.8%)의 몇 %를 차지하나 — 클수록 노이즈 손절 위험.
        tick_ratios = [krx_tick(t.buy_price) / t.buy_price / 0.008 * 100 for t in group]
        avg_tick = sum(tick_ratios) / len(tick_ratios)
        print(
            f"{label:<10}{len(group):>4}{wins:>3}{total / len(group):>+11,.0f}원"
            f"{total:>+11,.0f}원{avg_tick:>10.0f}%"
        )
    print("  틱/트레일링 = 1호가 움직임이 트레일링 폭(0.8%)의 몇 %인가.")
    print("  (예: 50% = 2틱 노이즈면 트레일링 발화 — 저가주일수록 노이즈 손절)")
    print()
    print("개별 왕복 (진입가 오름차순):")
    for t in sorted(trips, key=lambda t: t.buy_price):
        hold = (t.exit_ts - t.entry_ts).total_seconds() / 60.0
        print(
            f"  {t.day} {t.symbol} @{t.buy_price:>10,.0f}원 x{t.qty:>6,.0f}"
            f"  보유 {hold:>4.0f}분  순손익 {t.net_pnl:>+10,.0f}원"
        )


# ---------------------------------------------------------------- 메인


async def main() -> None:
    raw_trips = load_trips()
    trips = _merge_partial_exits(raw_trips)
    if not trips:
        print("라운드트립 없음 — logs/paper-day-*.log 확인.")
        return
    days = sorted({t.day for t in trips})
    print(f"원장: {len(trips)}왕복(부분익절 합산) / 거래일 {len(days)}일 ({days[0]} ~ {days[-1]})")
    print("⚠️ 표본이 작다 — 방향 참고용. 단독 결론 근거로 쓰지 말 것.")
    print()

    needed = sorted({(t.symbol, t.day) for t in trips})
    oldest = min(d for _, d in needed)
    bars: dict[tuple[str, date], list[tuple[datetime, float]]] = {}
    async with KiwoomRestClient() as client:
        for symbol in sorted({s for s, _ in needed}):
            by_day = await fetch_minute_closes(client, symbol, oldest_day=oldest)
            for s, d in needed:
                if s == symbol and d in by_day:
                    bars[(s, d)] = by_day[d]
    missing = [k for k in needed if k not in bars]
    if missing:
        print(f"분봉 미확보 {len(missing)}건: {missing} — 해당 반사실은 스킵됨.")
        print()

    section_a_cooldown(trips, bars)
    section_b_entry_delay(trips, bars)
    section_c_price_band(trips)


if __name__ == "__main__":
    asyncio.run(main())
