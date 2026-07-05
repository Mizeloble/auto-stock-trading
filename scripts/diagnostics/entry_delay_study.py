"""진입 지연 단축 반사실 검증 — 발굴 직후(09:02) 진입 vs 모멘텀 게이트 대기 (read-only).

무엇/왜: 비대칭 출구 검증(asymmetric_exit_study)의 새 단서 — 픽의 원재료 엣지
상당 부분이 개장 후 첫 10분에 소멸하는데, 현행 구조는 발굴 확정(~09:02) 후에도
지수 모멘텀 게이트가 15분 lookback history 를 기다리느라 첫 진입이 09:09~09:10
(7/1 실측: 발굴 09:01:50 → 첫 진입 09:09:58). 게이트 대기를 없애고 발굴 직후
진입했다면 어땠나 — 그 8분의 기회비용(또는 게이트의 필터 가치)을 실측한다.

방식:
- 표본: 실제 발굴 픽 전체(리포트 discovery_chosen, 2026-05-06~). 출구는 현행
  highvol 고정(로컬 최적 확정 — asymmetric_exit_study), **진입 시각만** 그리드:
  09:02(발굴 직후) / 09:05 / 09:10(현행 근사) / 09:15.
- 게이트의 '날 거르기' 효과 분리: 리포트 buys 로 픽별 실제 진입 여부를 판정해
  (a) 실제 진입된 픽 — 게이트를 결국 통과한 날: 그리드 차이 = 순수 타이밍 효과
  (b) 실제 진입 0 픽 — 유니버스 장전 후 게이트(또는 사이징)가 종일 차단:
      무조건 진입 시뮬 결과가 양수면 게이트가 이익을 막은 것, 음수면 지켜준 것.

한계(정직 고지):
- 표본 픽 34건 — 방향 참고용. 시뮬 체결 = 분봉 종가(봉내 급변 미반영).
- 09:02 진입은 "발굴이 09:02 전에 끝났고 모멘텀 게이트 없음"을 가정 — breadth
  settle 실측(09:01:50)과는 양립하지만, 게이트 제거 시 늘어날 헛매수(나쁜 날
  진입)는 (b) 부분집합이 근사할 뿐 완전히 재현하지 못한다.
- 진입 시각별 차이는 전부 '같은 출구 규칙' 아래의 비교 — 출구 상호작용 포함.

결과 (2026-07-04 실행, 픽 33건 · 실제 진입 26 / 미진입 7):
- **지지 없음(기각)** — 실제 진입 픽에서 09:02 는 평균만 근소 우위(-0.01% vs 현행
  -0.24%)이고 중앙(-1.15% vs -0.46%)·승률(6/26 vs 9/26)은 오히려 나쁨. 그리드가
  비단조(09:02 무난 / 09:05 최악 / 09:10 무난 / 09:15 나쁨) — 견고한 타이밍 엣지가
  아니라 소수 대박이 평균을 흔드는 노이즈 패턴. 09:02 조기 진입은 개장 변동에
  트레일링이 더 자주 잘리는 대가를 치른다.
- 게이트가 종일 차단한 픽(7건)은 +0.45%(09:02 기준) 기회비용 — 소폭이고, 그중
  6/16 004870 은 매매제한(어차피 매수 불가)이라 실질은 더 작다. 게이트 제거 근거 부족.
- '첫 10분 엣지'(asymmetric_exit_study)의 상당 부분은 09:00~09:02(개장 동시호가~
  첫 분봉) 구간 — 발굴 파이프라인(순위 집계 자체가 개장 후 데이터)이 구조적으로
  잡을 수 없는 구간이다. → 진입 시각 무변경.

실행: uv run python scripts/diagnostics/entry_delay_study.py
"""

from __future__ import annotations

import asyncio
import glob
import json
import statistics
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path

from entry_axes_study import GuardParams, fetch_minute_closes, simulate_entry

from auto_trading.kiwoom.rest import KiwoomRestClient

ENTRY_GRID = [time(9, 2), time(9, 5), time(9, 10), time(9, 15)]
CURRENT_LABEL = "09:10"  # 현행 근사 (실측 첫 진입 09:09~09:20)
# 현행 highvol 출구 고정 (출구 스택은 로컬 최적 확정 — 이 스터디는 진입 시각만 다룬다)
EXITS = GuardParams(stop=0.010, trail=0.008, partial=0.016, partial_ratio=0.5)
NOTIONAL = 3_300_000


@dataclass(frozen=True)
class Pick:
    day: date
    symbol: str
    actually_entered: bool  # 리포트 buys 에 그날 그 종목 매수가 존재했나


def load_picks() -> list[Pick]:
    picks: list[Pick] = []
    for p in sorted(glob.glob("data/reports/daily-*.json")):
        d = json.loads(Path(p).read_text())
        chosen = d.get("discovery_chosen") or []
        if not chosen:
            continue
        day = date.fromisoformat(Path(p).name[6:16])
        bought = {str(b.get("symbol")) for b in d.get("buys") or []}
        for s in chosen:
            picks.append(Pick(day=day, symbol=str(s), actually_entered=str(s) in bought))
    return picks


def _summ(label: str, vals: list[float]) -> str:
    if not vals:
        return f"{label:<12} 표본 0"
    wins = sum(1 for v in vals if v > 0)
    total = sum(vals) / 100.0 * NOTIONAL
    return (
        f"{label:<12} n={len(vals):>3}  평균 {statistics.mean(vals):+6.2f}%"
        f"  중앙 {statistics.median(vals):+6.2f}%  승률 {wins:>2}/{len(vals)}"
        f"  합계 {total:>+13,.0f}원"
    )


async def main() -> None:
    picks = load_picks()
    if not picks:
        print("픽 기록 없음 — data/reports/daily-*.json 확인.")
        return
    days = sorted({p.day for p in picks})
    symbols = sorted({p.symbol for p in picks})
    n_entered = sum(1 for p in picks if p.actually_entered)
    print(
        f"실제 픽 {len(picks)}건 / {len(days)}일 ({days[0]} ~ {days[-1]})"
        f" / 실제 진입 {n_entered}건 · 미진입(게이트 등 차단) {len(picks) - n_entered}건"
    )
    print("⚠️ 표본 작음 — 방향 참고용. 출구는 현행 고정, 진입 시각만 그리드.")
    print()

    oldest = min(days)
    bars: dict[tuple[str, date], list] = {}
    async with KiwoomRestClient() as client:
        for symbol in symbols:
            try:
                by_day = await fetch_minute_closes(client, symbol, oldest_day=oldest)
            except Exception:
                by_day = {}
            for p in picks:
                if p.symbol == symbol and p.day in by_day:
                    bars[(p.symbol, p.day)] = by_day[p.day]
    usable = [p for p in picks if (p.symbol, p.day) in bars]
    if len(usable) < len(picks):
        print(f"분봉 미확보 스킵 {len(picks) - len(usable)}건 (조회불가 — 생존편향 유의)")
        print()

    # (픽, 진입시각) → 순수익률 %
    grid: dict[time, dict[Pick, float]] = {t: {} for t in ENTRY_GRID}
    for p in usable:
        for t in ENTRY_GRID:
            sim = simulate_entry(bars[(p.symbol, p.day)], entry_at=t, params=EXITS)
            if sim is not None:
                grid[t][p] = sim.net_return_pct

    print("=" * 76)
    print("1. 전체 픽 — 진입 시각 그리드 (같은 픽·같은 출구, 시각만 이동)")
    print("=" * 76)
    for t in ENTRY_GRID:
        label = f"{t:%H:%M}" + (" ◀현행" if f"{t:%H:%M}" == CURRENT_LABEL else "")
        print(_summ(label, list(grid[t].values())))
    print()

    print("=" * 76)
    print("2. 실제 진입된 픽만 — 게이트를 결국 통과한 날 (순수 타이밍 효과)")
    print("=" * 76)
    for t in ENTRY_GRID:
        vals = [v for p, v in grid[t].items() if p.actually_entered]
        label = f"{t:%H:%M}" + (" ◀현행" if f"{t:%H:%M}" == CURRENT_LABEL else "")
        print(_summ(label, vals))
    print()

    print("=" * 76)
    print("3. 실제 진입 0 픽 — 장전됐지만 게이트·사이징이 종일 차단한 픽")
    print("=" * 76)
    blocked = [p for p in usable if not p.actually_entered]
    for p in blocked:
        cells = []
        for t in ENTRY_GRID:
            v = grid[t].get(p)
            cells.append(f"{t:%H:%M} {v:+.2f}%" if v is not None else f"{t:%H:%M} —")
        print(f"  {p.day} {p.symbol}: " + "  ".join(cells))
    for t in ENTRY_GRID:
        vals = [v for p, v in grid[t].items() if not p.actually_entered]
        print(_summ(f"{t:%H:%M}", vals))
    print("  양수면 게이트가 이익을 막은 것(기회비용), 음수면 손실을 지켜준 것.")
    print()
    print("읽는 법: 1·2번에서 09:02 가 09:10 보다 일관되게 좋으면 '지연 단축' 지지.")
    print("단, 게이트 제거의 대가(나쁜 날 진입)는 3번 부분집합이 근사한다.")


if __name__ == "__main__":
    asyncio.run(main())
