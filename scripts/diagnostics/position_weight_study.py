"""종목당 비중(max_position_weight 7%) 반사실 검증 — 2026-07-16 사용자 질문 (read-only).

무엇/왜: highvol 프로파일의 종목당 비중 7%(계좌 ~48M 기준 1종목 ~3.4M)가 적정한지,
  비중 그리드(4~20%)로 같은 거래 스트림을 재생해 실측한다. 손익은 사이즈에
  선형이지만 세 가지 비선형 상호작용이 답을 바꾼다:
  ① 일 매수한도 10M — 비중이 크면 상위 1~2종목이 한도를 독식(집중 리스크),
     작으면 한도가 남아 한낮 재진입(엣지 없음이 검증된 구간)이 통과된다.
  ② 일손실 브레이크 -10만(NET) — 비중이 크면 나쁜 날 더 일찍 걸려 손실을 자른다.
  ③ 종목당 2회·쿨다운 45분 — 비중과 무관하게 유지.

방법:
- 대상일: highvol 시대(2026-06-15 이후) 매수 체결이 있는 전 거래일.
- 실제 체결(로그) + 매수여력 거절 신호를 시각 순 병합 재생. 각 매수를
  budget = min(w x equity, 10M - 당일 누적 매수) 로 재사이징(factor), 매도는
  FIFO 매칭 lot 의 factor 로 비례 축소/확대. 거절 신호는 새 비중에서 예산이
  남으면 분봉 + PositionGuard 동일 출구 스택으로 추가 진입 시뮬
  (reentry_exposure_study 와 동일 기계, 브레이크·쿨다운·종목당 2회 유지).
- 검증: w=7% 재생이 각 일자 리포트의 비용 차감 PnL 을 재현하는지 대조.

한계(정직 고지):
- 실제 체결의 "진입 시각·종목 순서"는 고정 — 비중이 바뀌면 체결 임팩트·호가
  단위로 순서가 달라질 수 있으나 무시(분봉 종가 근사).
- 07-13 이후는 섀도우(가상) 기준선. 표본 8일 내외 — 방향 참고용.

실행: uv run python scripts/diagnostics/position_weight_study.py
"""

from __future__ import annotations

import asyncio
import glob
import json
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from entry_axes_study import (
    GuardParams,
    _round_trip_cost,
    fetch_minute_closes,
    guard_params_for,
    simulate_entry,
)
from reentry_exposure_study import (
    CAP,
    COOLDOWN,
    FALLBACK_EQUITY,
    LOSS_LIMIT,
    MAX_BUYS_PER_SYMBOL,
    MIN_ORDER,
    Fill,
    bar_price_at,
    day_equity,
    load_day,
)

from auto_trading.kiwoom.rest import KiwoomRestClient

WEIGHTS = [0.04, 0.05, 0.07, 0.10, 0.14, 0.20]
BASE_WEIGHT = 0.07
ERA_START = date(2026, 6, 15)  # highvol 프로파일(비중 7%·캡 10M) 적용 시작
OUT_PATH = Path("data/studies/position-weight-study.json")


def era_days() -> list[date]:
    """highvol 시대에 매수 체결이 있는 로그 일자."""
    days: list[date] = []
    for path_str in sorted(glob.glob("logs/paper-day-*.log")):
        day = date.fromisoformat(Path(path_str).name.replace("paper-day-", "").replace(".log", ""))
        if day < ERA_START:
            continue
        fills, _ = load_day(day)
        if any(f.side == "buy" for f in fills):
            days.append(day)
    return days


@dataclass
class Lot:
    qty_actual: float  # 실제 체결 수량 (매도 FIFO 매칭 기준)
    factor: float  # 재사이징 배율 (스케일 수량 = qty_actual x factor)
    buy_price: float


@dataclass
class DayOutcome:
    day: date
    weight: float
    net_pnl: float = 0.0
    buys: int = 0
    extra_entries: int = 0  # 거절 신호가 예산 회복으로 진입된 건
    brake_skipped: int = 0  # 브레이크로 소멸된 실제 매수
    cap_shrunk: int = 0  # 한도 잔여 부족으로 factor<1 로 줄어든 매수
    trades: list[str] = field(default_factory=list)


def replay(
    day: date,
    weight: float,
    fills: list[Fill],
    rejects: list[tuple[datetime, str]],
    bars: dict[str, list[tuple[datetime, float]]],
    params: GuardParams,
    equity: float,
) -> DayOutcome:
    out = DayOutcome(day=day, weight=weight)
    lots: dict[str, list[Lot]] = {}
    cum_bought = 0.0  # 당일 누적 매수대금 (현행과 동일한 누적 방식)
    realized = 0.0  # NET (비용 차감) — 브레이크 판정 기준
    buys_count: dict[str, int] = {}
    last_exit: dict[str, datetime] = {}
    cf_open: list[tuple[str, datetime, float]] = []  # (symbol, exit_ts, net_pnl)

    def settle_cf(now: datetime) -> None:
        nonlocal realized
        for item in [c for c in cf_open if c[1] <= now]:
            cf_open.remove(item)
            realized += item[2]
            last_exit[item[0]] = item[1]

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
                    continue
            if fill.side == "buy":
                if realized <= LOSS_LIMIT:
                    out.brake_skipped += 1
                    continue
                budget = min(weight * equity, CAP - cum_bought)
                if budget < MIN_ORDER or budget < price:
                    continue  # 당일 한도 소진 — 매수 소멸
                qty_scaled = float(int(budget // price))
                if qty_scaled <= 0:
                    continue
                factor = qty_scaled / fill.qty
                if budget < weight * equity - 1e-6:
                    out.cap_shrunk += 1
                lots.setdefault(fill.symbol, []).append(
                    Lot(qty_actual=fill.qty, factor=factor, buy_price=price)
                )
                cum_bought += qty_scaled * price
                buys_count[fill.symbol] = buys_count.get(fill.symbol, 0) + 1
                out.buys += 1
                out.trades.append(
                    f"{fill.symbol} {ts:%H:%M} 매수 x{qty_scaled:,.0f} (factor {factor:.2f})"
                )
                continue
            remaining = fill.qty
            queue = lots.get(fill.symbol) or []
            while remaining > 0 and queue:
                lot = queue[0]
                take = min(remaining, lot.qty_actual)
                scaled = take * lot.factor
                if scaled > 0:
                    realized += (price - lot.buy_price) * scaled - _round_trip_cost(
                        lot.buy_price, price, scaled
                    )
                remaining -= take
                lot.qty_actual -= take
                if lot.qty_actual <= 0:
                    queue.pop(0)
            if not queue:
                last_exit[fill.symbol] = ts
            continue

        symbol = payload
        assert isinstance(symbol, str)
        held = any(lot.factor > 0 for lot in lots.get(symbol, [])) or any(
            c[0] == symbol for c in cf_open
        )
        if held:
            continue
        if buys_count.get(symbol, 0) >= MAX_BUYS_PER_SYMBOL:
            continue
        prev_exit = last_exit.get(symbol)
        if prev_exit is not None and ts - prev_exit < COOLDOWN:
            continue
        if realized <= LOSS_LIMIT:
            continue
        day_bars = bars.get(symbol)
        if not day_bars:
            continue
        budget = min(weight * equity, CAP - cum_bought)
        entry_px = bar_price_at(day_bars, ts)
        if budget < MIN_ORDER or entry_px is None or budget < entry_px:
            continue
        qty = float(int(budget // entry_px))
        sim = simulate_entry(day_bars, entry_at=ts.time(), params=params, qty=qty)
        if sim is None:
            continue
        net_pnl = sim.net_return_pct / 100.0 * sim.entry_price * qty
        cf_open.append((symbol, sim.entry_ts + timedelta(minutes=sim.hold_min), net_pnl))
        cum_bought += sim.entry_price * qty
        buys_count[symbol] = buys_count.get(symbol, 0) + 1
        out.buys += 1
        out.extra_entries += 1
        out.trades.append(
            f"{symbol} {sim.entry_ts:%H:%M} 추가진입 x{qty:,.0f} ({sim.exit_reason}"
            f" {net_pnl:+,.0f}원)"
        )

    if stream:
        settle_cf(stream[-1][0] + timedelta(days=1))
    out.net_pnl = realized
    return out


async def main() -> None:
    days = era_days()
    if not days:
        print("대상일 없음 — logs/paper-day-*.log 확인.")
        return
    loaded = {day: load_day(day) for day in days}

    needed: set[tuple[str, date]] = set()
    for day, (fills, rejects) in loaded.items():
        needed |= {(sym, day) for _, sym in rejects}
        needed |= {(f.symbol, day) for f in fills if f.price is None}
    bars_by_key: dict[tuple[str, date], list[tuple[datetime, float]]] = {}
    if needed:
        oldest = min(d for _, d in needed)
        async with KiwoomRestClient() as client:
            for symbol in sorted({s for s, _ in needed}):
                by_day = await fetch_minute_closes(client, symbol, oldest_day=oldest)
                for sym, d in needed:
                    if sym == symbol and d in by_day:
                        bars_by_key[(sym, d)] = by_day[d]
        missing = sorted(k for k in needed if k not in bars_by_key)
        if missing:
            print(f"⚠️ 분봉 미확보 {len(missing)}건: {missing} — 해당 신호/체결 스킵")

    print("=" * 78)
    print("종목당 비중(max_position_weight) 그리드 반사실 — highvol 시대 재생")
    print(f"대상 {len(days)}일: {', '.join(d.isoformat() for d in days)}")
    print("규칙: 일 누적 매수한도 10M·일손실 -10만 브레이크·종목당 2회·쿨다운 45분 유지")
    print("=" * 78)

    results: dict[float, list[DayOutcome]] = {w: [] for w in WEIGHTS}
    for day in days:
        fills, rejects = loaded[day]
        day_bars = {sym: b for (sym, d), b in bars_by_key.items() if d == day}
        params = guard_params_for(day)
        equity = day_equity(day)
        for w in WEIGHTS:
            results[w].append(replay(day, w, fills, rejects, day_bars, params, equity))

    header = f"{'일자':<12}" + "".join(f"{f'{w:.0%}':>12}" for w in WEIGHTS)
    print(header)
    for idx, day in enumerate(days):
        row = f"{day.isoformat():<12}"
        for w in WEIGHTS:
            row += f"{results[w][idx].net_pnl:>+12,.0f}"
        print(row)
    print("-" * len(header))
    summary: dict[str, dict[str, float]] = {}
    for w in WEIGHTS:
        pnls = [o.net_pnl for o in results[w]]
        total = sum(pnls)
        worst = min(pnls)
        std = statistics.pstdev(pnls) if len(pnls) > 1 else 0.0
        extra = sum(o.extra_entries for o in results[w])
        shrunk = sum(o.cap_shrunk for o in results[w])
        braked = sum(o.brake_skipped for o in results[w])
        summary[f"{w:.2f}"] = {
            "total": total,
            "worst_day": worst,
            "std": std,
            "extra_entries": extra,
            "cap_shrunk_buys": shrunk,
            "brake_skipped_buys": braked,
        }
        base = " ◀ 현행" if abs(w - BASE_WEIGHT) < 1e-9 else ""
        print(
            f"{w:>4.0%}  합계 {total:>+12,.0f}원  최악일 {worst:>+11,.0f}원"
            f"  일표준편차 {std:>10,.0f}원  추가진입 {extra}건"
            f"  축소매수 {shrunk}건  브레이크소멸 {braked}건{base}"
        )
    print()
    print("검증(w=7% 재생 vs 리포트 비용차감 PnL):")
    base_idx = WEIGHTS.index(BASE_WEIGHT)
    for idx, day in enumerate(days):
        report_pnl = None
        for stem in (f"daily-{day.isoformat()}", f"shadow-{day.isoformat()}"):
            p = Path(f"data/reports/{stem}.json")
            if p.exists():
                report_pnl = json.loads(p.read_text()).get("realized_pnl_after_cost")
                break
        replayed = results[WEIGHTS[base_idx]][idx].net_pnl
        ref = f"{report_pnl:+,.0f}원" if isinstance(report_pnl, (int, float)) else "리포트 없음"
        print(f"  {day} 재생 {replayed:+,.0f}원 vs 리포트 {ref}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(
            {
                "study": "position-weight",
                "era_start": ERA_START.isoformat(),
                "days": [d.isoformat() for d in days],
                "weights": WEIGHTS,
                "matrix": {
                    f"{w:.2f}": [
                        {
                            "day": o.day.isoformat(),
                            "net_pnl": round(o.net_pnl),
                            "buys": o.buys,
                            "extra_entries": o.extra_entries,
                            "trades": o.trades,
                        }
                        for o in results[w]
                    ]
                    for w in WEIGHTS
                },
                "summary": summary,
                "equity_fallback": FALLBACK_EQUITY,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"저장: {OUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
