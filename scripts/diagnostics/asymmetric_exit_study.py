"""비대칭 출구 반사실 검증 — "부분익절 후 잔량만 완화해 꼬리를 보존" (read-only).

무엇/왜: 선정 알파 검증(selection_alpha_study) 결론 — 픽의 원재료 엣지는 오른꼬리
(평균 +1.15% vs 중앙 -0.18%)에 있는데 현행 출구(부분익절 +1.6% 절반 매도 후 잔량도
트레일링 0.8%)가 그 꼬리를 자른다. ATR 검증(atr_exit_study)에선 출구 '전면' 완화가
손해였다(패자도 오래 들고 감). 그래서 남은 가설 = **비대칭 출구**:
  - 진입 직후엔 현행 그대로 좁게 (패자는 빨리 자른다 — 검증된 장점 보존)
  - 부분익절(+1.6%)에 도달한 **잔량만** 규칙을 완화해 승자 꼬리를 태운다.

표본: 실제 발굴 픽 전체(리포트 discovery_chosen, 2026-05-06~) — 승자 꼬리가 논점이라
5월의 승자 픽들이 표본에 있어야 공정하다. 분봉(ka10080)으로 09:10 진입을 재현.

출구 규칙 (부분익절 도달 '후' 잔량에만 차이, 도달 전은 전부 동일):
- baseline   : 잔량도 트레일링 0.8% (현행 highvol 재현)
- post1.6/2.4/3.2 : 잔량 트레일링을 1.6/2.4/3.2% 로 완화 (고점 추적은 계속)
- hold       : 잔량 규칙 없음 — 15:15 청산까지 보유 (꼬리 전부 태우기 상한)
- be-floor   : 잔량 트레일링 없음 + 본전(진입가) 하회 시 즉시 청산 (반납 하한 방어)

한계(정직 고지):
- 표본 픽 34건(13일)·부분익절 도달 부분집합은 더 작다 — 방향 참고용.
- 진입 = 09:10 분봉 종가로 통일(실제 진입시각 근사). 시뮬 체결 = 분봉 종가.
- 부분익절 미도달 픽은 규칙 간 결과가 동일 — 차이는 전적으로 도달 부분집합에서 발생.
- 조회불가 종목(상장폐지 등)은 스킵·카운트 (생존편향: 꼬리 과대평가 방향일 수 있음).

결과 (2026-07-04 실행, 픽 33건·부분익절 도달 8건):
- **기각** — baseline(잔량도 0.8%)이 전 표본(-0.04%)·도달 부분집합(+2.44%) 모두 1위.
  잔량 완화(1.6~3.2%)·hold·본전방어·전구간 완화(sym 2.4%)·buy-hold 전부 열세.
  부분익절 후 가격은 대체로 되돌림 — 좁은 트레일링이 고점 근처를 정확히 회수한다.
- **원재료 재해석(새 단서)**: 봇의 실제 진입점(09:10)부터 재면 buy-hold 조차
  평균 -0.24%(비용 후) — 선정 알파의 +1.15%(09:00 시가 기준)와의 차이 ≈ 개장 후
  첫 10분. 즉 픽의 오른꼬리 엣지 상당 부분이 **진입 전에 이미 지나가는 구간**에 있다.
  (7/1 실측: 발굴 확정 09:01:50 → 첫 진입 09:09:58 — 모멘텀 게이트 history 대기 8분)
- 결론: 출구 스택은 이 표본에서 사실상 로컬 최적. 다음 가설 = 진입 지연 '단축'
  (발굴 직후 진입 — 모멘텀 게이트 대기 8분의 기회비용 반사실).

실행: uv run python scripts/diagnostics/asymmetric_exit_study.py
"""

from __future__ import annotations

import asyncio
import glob
import json
import statistics
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path

from entry_axes_study import _round_trip_cost, fetch_minute_closes

from auto_trading.kiwoom.rest import KiwoomRestClient

ENTRY_AT = time(9, 10)
LIQ_AT = time(15, 15)
# 현행 highvol 프로파일 고정 폭 — 전 일자 공통 baseline (일자별 프로파일 차이는
# 이 스터디의 관심사가 아니므로 통일. 실운영본=highvol, memory: regime-profile-drift).
STOP = 0.010
TRAIL_PRE = 0.008
PARTIAL_AT = 0.016
PARTIAL_RATIO = 0.5
POST_TRAIL_GRID = [0.016, 0.024, 0.032]
NOTIONAL = 3_300_000  # 원화 환산용 명목(포지션당 ~3.3M — 실운영 사이징 근사)


@dataclass(frozen=True)
class PickDay:
    day: date
    symbol: str


@dataclass(frozen=True)
class SimOut:
    net_pct: float  # 진입 notional 대비 순수익률 %
    partial_hit: bool
    exit_reason: str


def load_picks() -> list[PickDay]:
    picks: list[PickDay] = []
    for p in sorted(glob.glob("data/reports/daily-*.json")):
        d = json.loads(Path(p).read_text())
        day = date.fromisoformat(Path(p).name[6:16])
        for s in d.get("discovery_chosen") or []:
            picks.append(PickDay(day=day, symbol=str(s)))
    return picks


def simulate(
    day_bars: list[tuple[datetime, float]],
    *,
    post_trail: float | None,  # None = 규칙 없음(hold)
    be_floor: bool = False,  # 부분익절 후 본전 하회 시 청산
    pre_stop: float | None = STOP,  # None = 1단계 손절 없음
    pre_trail: float | None = TRAIL_PRE,  # None = 1단계 트레일링 없음
    partial_at: float | None = PARTIAL_AT,  # None = 부분익절 없음 (2단계 진입 안 함)
) -> SimOut | None:
    """2단계 출구: 부분익절 도달 전 = pre_* 규칙, 도달 후 잔량 = post 규칙.

    pre_*/partial_at 를 None 으로 끄면 buy-hold(규칙없음)·전구간 완화 같은
    대조군도 같은 시뮬레이터로 표현된다 (단일 구현 — 규칙 간 공정 비교).
    """
    bars = [(ts, px) for ts, px in day_bars if ts.time() >= ENTRY_AT]
    if not bars:
        return None
    entry_ts, entry = bars[0]
    if entry <= 0 or entry_ts.time() >= LIQ_AT:
        return None

    highest = entry
    remaining = 1.0
    partial_hit = False
    legs: list[tuple[float, float]] = []
    reason = "liquidate"

    for ts, px in bars[1:]:
        if ts.time() >= LIQ_AT:
            break
        highest = max(highest, px)
        if not partial_hit:
            # 1단계 — 기본은 현행 규칙 그대로 (패자 빨리 자르기 보존)
            if pre_stop is not None and 1.0 - px / entry >= pre_stop:
                legs.append((px, remaining))
                remaining = 0.0
                reason = "stop"
                break
            if pre_trail is not None and 1.0 - px / highest >= pre_trail:
                legs.append((px, remaining))
                remaining = 0.0
                reason = "trailing"
                break
            if partial_at is not None and px / entry - 1.0 >= partial_at:
                sell_qty = remaining * PARTIAL_RATIO
                legs.append((px, sell_qty))
                remaining -= sell_qty
                partial_hit = True
            continue
        # 2단계 — 잔량 규칙 (여기서만 baseline 과 갈라짐)
        if be_floor and px <= entry:
            legs.append((px, remaining))
            remaining = 0.0
            reason = "be_floor"
            break
        if post_trail is not None and 1.0 - px / highest >= post_trail:
            legs.append((px, remaining))
            remaining = 0.0
            reason = "post_trailing"
            break

    if remaining > 0:
        liq = [(ts, px) for ts, px in day_bars if ts.time() >= LIQ_AT]
        liq_px = liq[0][1] if liq else day_bars[-1][1]
        legs.append((liq_px, remaining))

    gross = sum((sell - entry) * q for sell, q in legs)
    cost = sum(_round_trip_cost(entry, sell, q) for sell, q in legs)
    return SimOut(
        net_pct=(gross - cost) / entry * 100.0,
        partial_hit=partial_hit,
        exit_reason=reason,
    )


RULES: list[tuple[str, dict]] = [
    ("baseline(잔량 0.8%)", {"post_trail": TRAIL_PRE}),
    *[(f"post {w * 100:.1f}%", {"post_trail": w}) for w in POST_TRAIL_GRID],
    ("hold(15:15까지)", {"post_trail": None}),
    ("be-floor(본전방어)", {"post_trail": None, "be_floor": True}),
    # 대조군 — 꼬리 절단 범인이 '부분익절 후'가 아니라 '도달 전 좁은 규칙'인지 판별.
    (
        "sym 2.4%(전구간)",
        {"post_trail": None, "pre_stop": None, "pre_trail": 0.024, "partial_at": None},
    ),
    (
        "buy-hold(규칙없음)",
        {"post_trail": None, "pre_stop": None, "pre_trail": None, "partial_at": None},
    ),
]


async def main() -> None:
    picks = load_picks()
    if not picks:
        print("픽 기록 없음 — data/reports/daily-*.json 확인.")
        return
    days = sorted({p.day for p in picks})
    symbols = sorted({p.symbol for p in picks})
    print(
        f"실제 픽 {len(picks)}건 / {len(days)}일 ({days[0]} ~ {days[-1]}) / 고유 {len(symbols)}종목"
    )
    print("⚠️ 표본 작음 — 방향 참고용. 진입 09:10 통일, 규칙 차이는 부분익절 도달 후에만.")
    print()

    oldest = min(days)
    bars: dict[tuple[str, date], list] = {}
    unavailable = 0
    async with KiwoomRestClient() as client:
        for symbol in symbols:
            try:
                by_day = await fetch_minute_closes(client, symbol, oldest_day=oldest)
            except Exception:
                by_day = {}
            for p in picks:
                if p.symbol == symbol:
                    series = by_day.get(p.day)
                    if series:
                        bars[(p.symbol, p.day)] = series
    usable = [p for p in picks if (p.symbol, p.day) in bars]
    unavailable = len(picks) - len(usable)
    if unavailable:
        print(f"분봉 미확보 스킵 {unavailable}건 (상장폐지·조회불가 — 생존편향 유의)")

    results: dict[str, list[SimOut]] = {label: [] for label, _ in RULES}
    for p in usable:
        for label, kw in RULES:
            out = simulate(bars[(p.symbol, p.day)], **kw)
            if out is not None:
                results[label].append(out)

    n_total = len(results[RULES[0][0]])
    n_partial = sum(1 for o in results[RULES[0][0]] if o.partial_hit)
    print(f"시뮬 대상 {n_total}건 / 부분익절(+{PARTIAL_AT * 100:.1f}%) 도달 {n_partial}건")
    print()

    print("=" * 76)
    print("전체 픽 기준 (부분익절 미도달 건은 규칙 간 동일 — 차이는 도달 건에서만)")
    print("=" * 76)
    print(f"{'규칙':<20}{'평균':>8}{'중앙':>8}{'승률':>8}{'합계(3.3M/건)':>16}")
    for label, _ in RULES:
        vals = [o.net_pct for o in results[label]]
        if not vals:
            continue
        wins = sum(1 for v in vals if v > 0)
        total_won = sum(vals) / 100.0 * NOTIONAL
        print(
            f"{label:<20}{statistics.mean(vals):>+7.2f}%{statistics.median(vals):>+7.2f}%"
            f"{wins:>5}/{len(vals)}{total_won:>+15,.0f}원"
        )
    print()

    print("=" * 76)
    print("부분익절 도달 건만 (규칙이 실제로 갈라지는 부분집합)")
    print("=" * 76)
    print(f"{'규칙':<20}{'평균':>8}{'중앙':>8}{'최대':>8}{'합계(3.3M/건)':>16}")
    base_partial_keys = [i for i, o in enumerate(results[RULES[0][0]]) if o.partial_hit]
    for label, _ in RULES:
        vals = [results[label][i].net_pct for i in base_partial_keys]
        if not vals:
            continue
        total_won = sum(vals) / 100.0 * NOTIONAL
        print(
            f"{label:<20}{statistics.mean(vals):>+7.2f}%{statistics.median(vals):>+7.2f}%"
            f"{max(vals):>+7.2f}%{total_won:>+15,.0f}원"
        )
    print()
    print("읽는 법: 'hold' 가 꼬리 태우기의 상한, 'be-floor' 는 반납 방어의 하한 성격.")
    print("post 폭이 baseline 보다 일관되게 좋고 hold 에 근접하면 비대칭 출구 가설 지지.")


if __name__ == "__main__":
    asyncio.run(main())
