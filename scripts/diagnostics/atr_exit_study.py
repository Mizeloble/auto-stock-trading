"""ATR 스케일 출구(트레일링) 반사실 검증 — 2026-07-04 진입 축 검증 후속 (read-only).

무엇/왜: entry_axes_study 결과 "문제는 진입 시각이 아니라 출구 구조" — 특히
삼성전자(저변동 대형주)가 고정 트레일링 0.8% 에 5왕복 4패(-121k), 001210(급등주)은
봉내 폭락 1건 -161k. 고정 폭 대신 **종목별 변동성(일봉 ATR14) 비례 출구 폭**이었다면
같은 진입에서 손익이 어땠나를 분봉 재시뮬로 검증한다.

실험 설계 (같은 진입 시각·같은 종목, 출구 규칙만 교체):
- baseline: 그날 레짐 프로파일의 고정 폭 (highvol: stop 1.0% / trail 0.8% / partial 1.6%)
- 변형 A "트레일링만 ATR": trail = k x atr_pct, stop·partial 은 baseline 고정.
- 변형 B "출구 3종 모두 ATR": baseline 비율(trail:stop:partial = 1 : 1.25 : 2)을
  유지한 채 전체를 k x atr_pct 로 스케일.
- k 그리드: 0.25 / 0.5 / 0.75 / 1.0. atr_pct = 전일까지 일봉 ATR(14) / 전일 종가
  (당일 봉 제외 — 룩어헤드 방지).
- 폭 클램프: [0.3%, 6.0%] — 왕복비용(~0.33%)보다 좁은 출구와 일중 전략에서 무의미하게
  넓은 출구를 배제. 클램프 발동은 표에 표시.

한계(정직 고지):
- 표본 4거래일 11진입 — k 그리드는 과적합 위험이 커서 "방향"만 읽는다.
- 시뮬 체결가 = 분봉 종가 (PositionGuard 와 동일 판정 근사). 봉내 급락 오버슈트는
  고정/ATR 어느 쪽도 동일하게 당한다(그 문제는 틱 기반 가드 축 — PR #157 수집 중).
- 재사용: 원장·분봉·시뮬레이터는 entry_axes_study 의 것을 그대로 import (드리프트 방지).

결과 (2026-07-04 실행, 4거래일 11진입):
- **기각** — 모든 ATR 변형이 baseline(고정 폭)보다 나빴다 (합계 -15k → A -96k~-141k,
  B -304k~-603k). 원인: 이 시장(연속 HIGH_VOL)에선 종목 간 ATR 차이가 거의 없다
  (삼성전자 6.4~7.5% vs 급등주 8.4~9.5% — 전 종목 고변동). "대형주=저변동" 전제가
  성립하지 않아 ATR 스케일링은 사실상 '모든 출구를 2~9배 넓히기'가 됐고, whipsaw
  장에서 넓은 출구는 반납만 커진다. 현행 좁은 고정 폭(0.8%)이 테스트 규칙 중 최선.
- 재검토 조건: 시장이 정상 변동성으로 복귀해 종목 간 ATR 이 실제로 갈라질 때.

실행: uv run python scripts/diagnostics/atr_exit_study.py
"""

from __future__ import annotations

import asyncio
from datetime import date

from entry_axes_study import (
    GuardParams,
    Trip,
    _merge_partial_exits,
    fetch_minute_closes,
    guard_params_for,
    load_trips,
    simulate_entry,
)

from auto_trading.kiwoom.rest import KiwoomRestClient
from auto_trading.market.chart import get_daily_bars
from auto_trading.strategy import indicators as ind

K_GRID = [0.25, 0.5, 0.75, 1.0]
WIDTH_MIN = 0.003  # 0.3% — 왕복비용(~0.33%)보다 좁은 출구는 실무상 무의미
WIDTH_MAX = 0.060  # 6.0% — 일중 전략에서 사실상 '출구 없음'
ATR_PERIOD = 14


def clamp_width(width: float) -> tuple[float, bool]:
    clamped = min(max(width, WIDTH_MIN), WIDTH_MAX)
    return clamped, clamped != width


async def fetch_atr_pct(
    client: KiwoomRestClient, symbol: str, days: list[date]
) -> dict[date, float | None]:
    """일자별 atr_pct = 전일까지 일봉 ATR(14) / 전일 종가. 당일 봉 제외(룩어헤드 방지)."""
    bars = await get_daily_bars(client, symbol, max_pages=2)
    bars.sort(key=lambda b: b.ts)
    out: dict[date, float | None] = {}
    for day in days:
        prior = [b for b in bars if b.ts.date() < day]
        if len(prior) < ATR_PERIOD + 1:
            out[day] = None
            continue
        window = prior[-(ATR_PERIOD * 3) :]
        atr_val = ind.atr(
            [b.high for b in window],
            [b.low for b in window],
            [b.close for b in window],
            period=ATR_PERIOD,
        )
        last_close = window[-1].close
        out[day] = (atr_val / last_close) if atr_val and last_close > 0 else None
    return out


def variant_params(
    base: GuardParams, atr_pct: float, k: float, *, scale_all: bool
) -> tuple[GuardParams, bool]:
    """변형 A(트레일링만) / B(3종 모두, baseline 비율 유지). 반환: (파라미터, 클램프 발동)."""
    trail, c1 = clamp_width(k * atr_pct)
    if not scale_all:
        return GuardParams(
            stop=base.stop, trail=trail, partial=base.partial, partial_ratio=base.partial_ratio
        ), c1
    stop_ratio = base.stop / base.trail if base.trail > 0 else 1.25
    partial_ratio_w = base.partial / base.trail if base.trail > 0 else 2.0
    stop, c2 = clamp_width(trail * stop_ratio)
    partial, c3 = clamp_width(trail * partial_ratio_w)
    return GuardParams(
        stop=stop, trail=trail, partial=partial, partial_ratio=base.partial_ratio
    ), c1 or c2 or c3


async def main() -> None:
    trips = _merge_partial_exits(load_trips())
    if not trips:
        print("라운드트립 없음 — logs/paper-day-*.log 확인.")
        return
    days = sorted({t.day for t in trips})
    print(f"원장: {len(trips)}진입 / 거래일 {len(days)}일 ({days[0]} ~ {days[-1]})")
    print("⚠️ 표본이 작다 — k 그리드는 과적합 위험. 방향 참고용.")
    print()

    needed = sorted({(t.symbol, t.day) for t in trips})
    oldest = min(d for _, d in needed)
    symbols = sorted({s for s, _ in needed})

    bars: dict[tuple[str, date], list] = {}
    atr_by_symbol: dict[str, dict[date, float | None]] = {}
    async with KiwoomRestClient() as client:
        for symbol in symbols:
            by_day = await fetch_minute_closes(client, symbol, oldest_day=oldest)
            for s, d in needed:
                if s == symbol and d in by_day:
                    bars[(s, d)] = by_day[d]
            atr_by_symbol[symbol] = await fetch_atr_pct(
                client, symbol, sorted({d for s, d in needed if s == symbol})
            )

    print("종목별 전일 기준 일봉 ATR% (출구 폭의 스케일 원천):")
    for symbol in symbols:
        for day, atr_pct in sorted(atr_by_symbol[symbol].items()):
            shown = f"{atr_pct * 100:.2f}%" if atr_pct is not None else "산출불가"
            print(f"  {day} {symbol}: {shown}")
    print()

    # 트립별: baseline 재현 + 변형 AxK + 변형 BxK
    labels = ["baseline"]
    labels += [f"A k={k}" for k in K_GRID]
    labels += [f"B k={k}" for k in K_GRID]
    totals_won: dict[str, float] = dict.fromkeys(labels, 0.0)
    totals_pct: dict[str, list[float]] = {label: [] for label in labels}
    skipped = 0

    def run_case(t: Trip, label: str, params: GuardParams, note: str = "") -> str:
        day_bars = bars.get((t.symbol, t.day))
        sim = (
            simulate_entry(day_bars, entry_at=t.entry_ts.time(), params=params)
            if day_bars
            else None
        )
        if sim is None:
            return f"{label} —"
        won = sim.net_return_pct / 100.0 * t.buy_price * t.qty
        totals_won[label] += won
        totals_pct[label].append(sim.net_return_pct)
        return f"{label} {sim.net_return_pct:+.2f}%{note}"

    print("트립별 결과 (순수익률 %, 비용 차감 · won 합산은 실제 수량 기준):")
    for t in trips:
        atr_pct = atr_by_symbol.get(t.symbol, {}).get(t.day)
        base = guard_params_for(t.day)
        cells = [run_case(t, "baseline", base)]
        if atr_pct is None:
            skipped += 1
            print(f"  {t.day} {t.symbol} @{t.buy_price:,.0f}: ATR 산출불가 — 변형 스킵")
            continue
        for k in K_GRID:
            params, clamped = variant_params(base, atr_pct, k, scale_all=False)
            cells.append(run_case(t, f"A k={k}", params, "†" if clamped else ""))
        for k in K_GRID:
            params, clamped = variant_params(base, atr_pct, k, scale_all=True)
            cells.append(run_case(t, f"B k={k}", params, "†" if clamped else ""))
        print(
            f"  {t.day} {t.symbol} {t.entry_ts:%H:%M} @{t.buy_price:>9,.0f} "
            f"(ATR {atr_pct * 100:.1f}%) 실제 {t.net_pnl:+,.0f}원"
        )
        print("    " + "  ".join(cells))

    print()
    print("=" * 72)
    print("집계 — 같은 진입 11건, 출구 규칙별 (won 은 실제 수량 환산 합계)")
    print("=" * 72)
    print(f"{'규칙':<12}{'순손익 합':>14}{'평균 수익률':>10}{'승률':>8}")
    for label in labels:
        vals = totals_pct[label]
        if not vals:
            continue
        wins = sum(1 for v in vals if v > 0)
        print(
            f"{label:<12}{totals_won[label]:>+13,.0f}원"
            f"{sum(vals) / len(vals):>+9.2f}%{wins:>5}/{len(vals)}"
        )
    if skipped:
        print(f"(ATR 산출불가로 변형 스킵 {skipped}건 — baseline 에만 포함)")
    print()
    print("읽는 법: 변형 A = 트레일링만 kxATR, B = 출구 3종 모두 kxATR(비율 유지).")
    print("† = 폭 클램프(0.3%~6.0%) 발동. baseline 은 그날 레짐 프로파일 고정 폭 재현.")


if __name__ == "__main__":
    asyncio.run(main())
