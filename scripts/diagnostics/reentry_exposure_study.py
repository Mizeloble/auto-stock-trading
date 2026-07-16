"""재매수 허용(노출 기반 일한도) 반사실 검증 — 2026-07-16 사용자 제안 (read-only).

무엇/왜: daily_notional_limit(일 *누적* 매수대금 10M 하드캡)를 "미청산 매입원가 기준"
  으로 바꿨다면 — 즉 매도로 포지션을 정리한 만큼 당일 재매수 여력이 회복된다면 —
  손익이 어땠는지 실측한다. 2026-06 반사실(6/15·6/17 각 1일)의 표본 확장판.

방법:
- 대상일: 매수여력 사유(_is_buying_power_reject)로 신호가 거절된 날 전부.
  crash 프로파일(1원 캡) 차단일(05-29·06-12)은 전면정지라 성격이 달라 제외.
- 거절 신호는 실제 전략 신호다 — risk manager 는 쿨다운·종목당 횟수·상관 체크를
  모두 통과한 뒤 마지막에 예산을 보므로, "게이트 통과 가정" 없이 재생 가능.
- 재생: 신호 시각 순으로 (보유중복 → 종목당 2회 → 쿨다운 45분 → 일손실 -100k
  브레이크 → 잔여예산 = 10M - 실제·반사실 미청산 매입원가) 를 그대로 적용,
  통과분만 ka10080 분봉 위에서 PositionGuard 동일 출구 스택으로 시뮬
  (entry_axes_study.simulate_entry 재사용 — stop→trailing→partial→15:15 청산).

한계(정직 고지):
- 분봉 종가 체결 근사 + 비용 상수(수수료·세금·슬리피지) — 실슬리피지 미반영.
- 반사실 부분익절의 중간 예산 회복은 무시(전량 청산 시점에 일괄 회복 — 보수적).
- 07-13·07-15 는 섀도우(가상) 일이라 "실제" 기준선 자체가 시뮬레이션.
- 표본 4일 — 방향 참고용. 단독 결론 근거로 쓰지 말 것.

실행: uv run python scripts/diagnostics/reentry_exposure_study.py
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from entry_axes_study import (
    GuardParams,
    _round_trip_cost,
    _ts_kst,
    fetch_minute_closes,
    guard_params_for,
    simulate_entry,
)

from auto_trading.kiwoom.rest import KiwoomRestClient
from auto_trading.reports.daily.extract import _is_buying_power_reject
from auto_trading.reports.log_parser import parse_log

DAYS = [date(2026, 6, 15), date(2026, 6, 17), date(2026, 7, 13), date(2026, 7, 15)]
CAP = 10_000_000.0
POS_WEIGHT = 0.07
COOLDOWN = timedelta(minutes=45)
MAX_BUYS_PER_SYMBOL = 2
LOSS_LIMIT = -100_000.0
MIN_ORDER = 10_000.0
FALLBACK_EQUITY = 48_400_000.0
REPORT_DIR = Path("data/reports")
OUT_PATH = Path("data/studies/reentry-exposure-study.json")


@dataclass
class Fill:
    ts: datetime
    symbol: str
    side: str  # buy / sell
    qty: float
    price: float | None  # liquidate_at_reached 는 price 없음 → 분봉으로 근사


@dataclass
class CfTrade:
    symbol: str
    entry_ts: datetime
    exit_ts: datetime
    entry_price: float
    qty: float
    exit_reason: str
    net_pnl: float


def load_day(day: date) -> tuple[list[Fill], list[tuple[datetime, str]]]:
    """로그 → (실제 체결, 매수여력 거절 신호). 둘 다 시각 오름차순."""
    events = parse_log(Path(f"logs/paper-day-{day.isoformat()}.log"))
    fills: list[Fill] = []
    for rec in (*events.orders_accepted, *events.orders_simulated):
        ts = _ts_kst(rec)
        side = str(rec.get("side", "")).lower()
        if ts is None or side not in ("buy", "sell"):
            continue
        price = rec.get("price")
        fills.append(
            Fill(
                ts=ts,
                symbol=str(rec.get("symbol", "")),
                side=side,
                qty=float(rec.get("qty", rec.get("quantity", 0)) or 0),
                price=float(price) if isinstance(price, (int, float)) else None,
            )
        )
    for rec in (*events.liquidations_accepted, *events.liquidations_simulated):
        ts = _ts_kst(rec)
        if ts is None:
            continue
        price = rec.get("sell_price", rec.get("price"))
        fills.append(
            Fill(
                ts=ts,
                symbol=str(rec.get("symbol", "")),
                side="sell",
                qty=float(rec.get("qty", rec.get("quantity", 0)) or 0),
                price=float(price) if isinstance(price, (int, float)) else None,
            )
        )
    fills.sort(key=lambda f: f.ts)

    rejects = [
        (ts, str(rec.get("symbol", "")))
        for rec in events.signal_rejected
        if _is_buying_power_reject(str(rec.get("reason") or ""))
        and str(rec.get("side", "")).lower() == "buy"
        and (ts := _ts_kst(rec)) is not None
    ]
    rejects.sort(key=lambda r: r[0])
    return fills, rejects


def day_equity(day: date) -> float:
    """리포트의 traded_notional/turnover_ratio 로 그날 평가액 역산. 없으면 폴백."""
    for stem in (f"daily-{day.isoformat()}", f"shadow-{day.isoformat()}"):
        path = REPORT_DIR / f"{stem}.json"
        if not path.exists():
            continue
        data = json.loads(path.read_text())
        traded, turnover = data.get("traded_notional"), data.get("turnover_ratio")
        if traded and turnover:
            return float(traded) / float(turnover)
    return FALLBACK_EQUITY


def bar_price_at(bars: list[tuple[datetime, float]], ts: datetime) -> float | None:
    """ts 이후 첫 봉 종가 (없으면 마지막 봉 — 15:15 청산 근사용)."""
    for bar_ts, px in bars:
        if bar_ts >= ts:
            return px
    return bars[-1][1] if bars else None


def replay_day(
    day: date,
    fills: list[Fill],
    rejects: list[tuple[datetime, str]],
    bars: dict[str, list[tuple[datetime, float]]],
    params: GuardParams,
    equity: float,
) -> dict:
    """실제 체결 + 거절 신호를 시각 순으로 재생, 노출 기반 한도로 반사실 진입 시뮬."""
    lots: dict[str, list[list[float]]] = {}  # symbol -> [[qty, buy_price], ...] FIFO
    outstanding = 0.0  # 실제 + 반사실 미청산 매입원가
    realized = 0.0  # 실제 + 반사실 실현손익(비용 차감)
    buys_count: dict[str, int] = {}
    last_exit: dict[str, datetime] = {}
    cf_open: list[CfTrade] = []
    cf_done: list[CfTrade] = []
    skipped = {
        "holding": 0,
        "max_buys": 0,
        "cooldown": 0,
        "loss_brake": 0,
        "budget": 0,
        "no_bars": 0,
    }
    actual_realized = 0.0

    def settle_cf(now: datetime) -> None:
        nonlocal outstanding, realized
        for trade in [t for t in cf_open if t.exit_ts <= now]:
            cf_open.remove(trade)
            cf_done.append(trade)
            outstanding -= trade.entry_price * trade.qty
            realized += trade.net_pnl
            last_exit[trade.symbol] = trade.exit_ts

    # fills 와 rejects 를 시각 순으로 병합 재생 (fill 이 같은 시각이면 먼저)
    stream: list[tuple[datetime, int, object]] = [(f.ts, 0, f) for f in fills]
    stream += [(ts, 1, sym) for ts, sym in rejects]
    stream.sort(key=lambda e: (e[0], e[1]))

    for ts, kind, payload in stream:
        settle_cf(ts)
        if kind == 0:
            fill = payload
            assert isinstance(fill, Fill)
            price = fill.price
            if price is None:
                price = bar_price_at(bars.get(fill.symbol, []), fill.ts)
                if price is None:
                    continue  # 분봉도 없으면 손익 추적 불가 — 스킵(고지)
            if fill.side == "buy":
                lots.setdefault(fill.symbol, []).append([fill.qty, price])
                outstanding += fill.qty * price
                buys_count[fill.symbol] = buys_count.get(fill.symbol, 0) + 1
                continue
            remaining = fill.qty
            queue = lots.get(fill.symbol) or []
            while remaining > 0 and queue:
                lot = queue[0]
                take = min(remaining, lot[0])
                pnl = (price - lot[1]) * take - _round_trip_cost(lot[1], price, take)
                realized += pnl
                actual_realized += pnl
                outstanding -= lot[1] * take
                remaining -= take
                lot[0] -= take
                if lot[0] <= 0:
                    queue.pop(0)
            if not queue:
                last_exit[fill.symbol] = ts
            continue

        symbol = payload
        assert isinstance(symbol, str)
        if lots.get(symbol) or any(t.symbol == symbol for t in cf_open):
            skipped["holding"] += 1
            continue
        if buys_count.get(symbol, 0) >= MAX_BUYS_PER_SYMBOL:
            skipped["max_buys"] += 1
            continue
        prev_exit = last_exit.get(symbol)
        if prev_exit is not None and ts - prev_exit < COOLDOWN:
            skipped["cooldown"] += 1
            continue
        if realized <= LOSS_LIMIT:
            skipped["loss_brake"] += 1
            continue
        day_bars = bars.get(symbol)
        if not day_bars:
            skipped["no_bars"] += 1
            continue
        budget = min(equity * POS_WEIGHT, CAP - outstanding)
        entry_px = bar_price_at(day_bars, ts)
        if budget < MIN_ORDER or entry_px is None or budget < entry_px:
            skipped["budget"] += 1
            continue
        qty = float(int(budget // entry_px))
        sim = simulate_entry(day_bars, entry_at=ts.time(), params=params, qty=qty)
        if sim is None:
            skipped["budget"] += 1
            continue
        net_pnl = sim.net_return_pct / 100.0 * sim.entry_price * qty
        trade = CfTrade(
            symbol=symbol,
            entry_ts=sim.entry_ts,
            exit_ts=sim.entry_ts + timedelta(minutes=sim.hold_min),
            entry_price=sim.entry_price,
            qty=qty,
            exit_reason=sim.exit_reason,
            net_pnl=net_pnl,
        )
        cf_open.append(trade)
        outstanding += sim.entry_price * qty
        buys_count[symbol] = buys_count.get(symbol, 0) + 1

    settle_cf(datetime.max.replace(tzinfo=stream[-1][0].tzinfo) if stream else datetime.max)
    cf_pnl = sum(t.net_pnl for t in cf_done)
    return {
        "day": day.isoformat(),
        "actual_net_pnl": actual_realized,
        "cf_trades": [
            {
                "symbol": t.symbol,
                "entry": t.entry_ts.strftime("%H:%M"),
                "exit": t.exit_ts.strftime("%H:%M"),
                "entry_price": t.entry_price,
                "qty": t.qty,
                "exit_reason": t.exit_reason,
                "net_pnl": round(t.net_pnl),
            }
            for t in cf_done
        ],
        "cf_extra_pnl": cf_pnl,
        "new_rule_day_pnl": actual_realized + cf_pnl,
        "rejects_total": len(rejects),
        "skipped": skipped,
    }


async def main() -> None:
    per_day: list[dict] = []
    needed: set[tuple[str, date]] = set()
    loaded: dict[date, tuple[list[Fill], list[tuple[datetime, str]]]] = {}
    for day in DAYS:
        fills, rejects = load_day(day)
        loaded[day] = (fills, rejects)
        needed |= {(sym, day) for _, sym in rejects}
        needed |= {(f.symbol, day) for f in fills if f.price is None}

    oldest = min(d for _, d in needed)
    bars_by_key: dict[tuple[str, date], list[tuple[datetime, float]]] = {}
    async with KiwoomRestClient() as client:
        for symbol in sorted({s for s, _ in needed}):
            by_day = await fetch_minute_closes(client, symbol, oldest_day=oldest)
            for sym, d in needed:
                if sym == symbol and d in by_day:
                    bars_by_key[(sym, d)] = by_day[d]
    missing = sorted(k for k in needed if k not in bars_by_key)
    if missing:
        print(f"⚠️ 분봉 미확보 {len(missing)}건: {missing} — 해당 신호는 스킵(no_bars)")

    print("=" * 76)
    print("재매수 허용(노출 기반 일한도 10M) 반사실 — 매수여력 차단일 4일 재생")
    print("규칙: 미청산 매입원가 기준 한도 + 쿨다운45분·종목당2회·일손실 -10만 브레이크 유지")
    print("=" * 76)
    total_actual = total_cf = 0.0
    for day in DAYS:
        fills, rejects = loaded[day]
        day_bars = {sym: b for (sym, d), b in bars_by_key.items() if d == day}
        result = replay_day(day, fills, rejects, day_bars, guard_params_for(day), day_equity(day))
        per_day.append(result)
        total_actual += result["actual_net_pnl"]
        total_cf += result["cf_extra_pnl"]
        tag = "(섀도우)" if day >= date(2026, 7, 13) else "(페이퍼)"
        print(f"\n■ {day} {tag} — 차단 신호 {result['rejects_total']}건")
        print(f"  실제 순손익(비용 차감): {result['actual_net_pnl']:+,.0f}원")
        if not result["cf_trades"]:
            reasons = ", ".join(f"{k}={v}" for k, v in result["skipped"].items() if v)
            print(f"  반사실 추가 진입 0건 (스킵 사유: {reasons or '없음'})")
        for t in result["cf_trades"]:
            print(
                f"  + {t['symbol']} {t['entry']}→{t['exit']} @{t['entry_price']:,.0f}원"
                f" x{t['qty']:,.0f} ({t['exit_reason']}) {t['net_pnl']:+,.0f}원"
            )
        print(
            f"  ⇒ 추가 손익 {result['cf_extra_pnl']:+,.0f}원 → 그날 합계"
            f" {result['new_rule_day_pnl']:+,.0f}원"
        )

    print("\n" + "-" * 76)
    print(f"합계 — 실제 {total_actual:+,.0f}원 → 재매수 허용 시 {total_actual + total_cf:+,.0f}원")
    print(f"Δ(재매수 허용의 순효과): {total_cf:+,.0f}원")
    print("주의: 4일 표본 + 분봉 근사 — 방향 참고용. 07-13·07-15 는 섀도우 기준선.")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(
            {
                "study": "reentry-exposure",
                "rule": "daily cap on outstanding cost basis (sells free budget)",
                "days": per_day,
                "total_actual": total_actual,
                "total_cf_extra": total_cf,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"저장: {OUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
