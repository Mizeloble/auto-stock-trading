"""보유 구조 스윕 — 저변동성 장중 선정 + 오버나이트 구조 판정 기록 (read-only).

무엇/왜: 풀 열세(-1.78%p, framework_verdict_study)가 확정된 뒤, "그럼 어떤 구조면
되는가" 를 사전 등록으로 판정한 기록. fdr 일봉 패널(가격은 키움 차트 TR 과 일치
실측, 유니버스 구성은 생존편향 있음 — 절대 수준 낙관 방향).

판정 결과 (최초 확정 2026-08-17, 506일 — fdr 캐시가 자라면 재실행 수치는 소폭
이동하나 판정은 동일했다. 예: 저변동성 알파 +0.092 -> +0.086%p, B1 순익 -0.059 -> -0.062):
  [가] 저변동성 top3 · 장중(시가->종가) — **기각**
      알파 +0.092%p (CI [-0.002, +0.204] — 경계선, low-vol anomaly 방향과 일치하나 미달)
      비용(0.316%) 후 절대 순익 -0.312% (CI [-0.391, -0.227]) — 확정 적자.
  [나] 오버나이트 구조 — **전 형태 기각/베타**
      B1 매일 왕복(종가->익일시가): 갭 +0.257 - 비용 0.316 = -0.059%/일 (CI 0 포함).
         **오버나이트 엣지는 실재하지만 왕복비용이 거의 정확히 그걸 먹는다.**
      B2 N일 보유: +0.10~0.14%/일 양수지만 선정 없는 유니버스 보유 = 베타.
      B3 저변동성 오버나이트: 갭 +0.008%/일 — 저변동성 종목은 갭 자체가 없음.
      갭 리스크: 종목-일 1% 분위 -5.17%, 최악 -30% — 장전엔 팔 수 없어 손절 불가.

후속(같은 날, 별도 검정 — 기록만):
  - 멀티팩터 결합 C2(low_vol+reversal): 알파 +0.151%p 로 결합 효과 실재하나
    비용 후 -0.252% 확정 적자 -> 기각.
  - '강한 마감'(인샘플 +0.392%p 관찰)은 OOS(2023-06~2024-06, 254일)에서
    **-0.491%p (CI [-0.880, -0.049])** 로 역방향 유의 -> 기각. 사후 부호 뒤집기의
    함정 실례 — 사전 등록 규율이 실손실을 막았다.

실행: uv run python scripts/diagnostics/structure_sweep_study.py
"""

from __future__ import annotations

import itertools
import statistics
from datetime import date, timedelta

from indicator_harness import build_panel, build_universe, moving_block_ci

COST = 0.316  # 실측 왕복비용 (framework_verdict_study)
VOL_WIN = 20
PICK_K = 3


def _ci_str(ci: tuple[float, float] | None) -> str:
    return f"[{ci[0]:+.3f}, {ci[1]:+.3f}]" if ci else "CI 불가"


def main() -> None:
    end = date.today()
    start = end - timedelta(days=(365 * 2) + 60)
    panel = build_panel(build_universe(), start.isoformat(), end.isoformat())
    print(f"패널 {len(panel)}종목 (fdr, 가격=키움 차트와 일치 확인)\n")

    index = {sym: {b.day: i for i, b in enumerate(bars)} for sym, bars in panel.items()}
    all_days = sorted({b.day for bars in panel.values() for b in bars})

    uni_intra: dict[date, float] = {}
    lv_intra: dict[date, float] = {}
    uni_over: dict[date, float] = {}
    lv_over: dict[date, float] = {}
    over_symdays: list[float] = []

    for day in all_days:
        rows = []  # (vol20, intra, over)
        for _sym, bars in panel.items():
            pos = index[_sym].get(day)
            if pos is None or pos < VOL_WIN + 1:
                continue
            today, prev = bars[pos], bars[pos - 1]
            if today.open <= 0 or prev.close <= 0:
                continue
            closes = [b.close for b in bars[pos - VOL_WIN - 1 : pos]]
            rets = [(b / a - 1) * 100 for a, b in itertools.pairwise(closes) if a > 0]
            if len(rets) < VOL_WIN - 2:
                continue
            rows.append(
                (
                    statistics.stdev(rets),
                    (today.close / today.open - 1) * 100,
                    (today.open / prev.close - 1) * 100,
                )
            )
        if len(rows) < 100:
            continue
        intras = [r[1] for r in rows]
        if all(abs(x) < 1e-12 for x in intras):
            continue  # 휴장
        uni_intra[day] = statistics.fmean(intras)
        uni_over[day] = statistics.fmean([r[2] for r in rows])
        over_symdays.extend(r[2] for r in rows)
        by_vol = sorted(rows, key=lambda r: r[0])[:PICK_K]
        lv_intra[day] = statistics.fmean([r[1] for r in by_vol])
        lv_over[day] = statistics.fmean([r[2] for r in by_vol])

    days = sorted(uni_intra)

    print("=" * 66)
    print(f"[가] 저변동성 top{PICK_K} · 장중(시가->종가) · {len(days)}일")
    print("=" * 66)
    u = [uni_intra[d] for d in days]
    lv = [lv_intra[d] for d in days]
    alpha = [lv_intra[d] - uni_intra[d] for d in days]
    net = [v - COST for v in lv]
    print(
        f"유니버스 평균 {statistics.fmean(u):+.3f}%/일 · 저변동성 top3 {statistics.fmean(lv):+.3f}%/일"
    )
    ci_a, ci_n = moving_block_ci(alpha), moving_block_ci(net)
    c1 = ci_a is not None and ci_a[0] > 0
    c2 = ci_n is not None and ci_n[0] > 0
    print(f"  ① 알파 {statistics.fmean(alpha):+.3f}%p · CI {_ci_str(ci_a)} {'✅' if c1 else '❌'}")
    print(
        f"  ② 비용 후 순익 {statistics.fmean(net):+.3f}% · CI {_ci_str(ci_n)} {'✅' if c2 else '❌'}"
    )
    print(f"  -> **{'채택 후보' if c1 and c2 else '기각'}**")

    print()
    print("=" * 66)
    print(f"[나] 오버나이트 구조 · {len(days)}일")
    print("=" * 66)
    uo = [uni_over[d] for d in days]
    b1_net = [v - COST for v in uo]
    ci_b1 = moving_block_ci(b1_net)
    ok_b1 = ci_b1 is not None and ci_b1[0] > 0
    print("B1. 매일 왕복 (종가 매수 -> 익일 시가 매도, 유니버스 평균):")
    print(
        f"    갭 {statistics.fmean(uo):+.3f}%/일 - 비용 {COST}% = 순익 "
        f"{statistics.fmean(b1_net):+.3f}%/일 · CI {_ci_str(ci_b1)} {'✅' if ok_b1 else '❌'}"
    )
    lvo = [lv_over[d] for d in days if d in lv_over]
    print(
        f"B3. (탐색) 저변동성 top3 오버나이트: 갭 {statistics.fmean(lvo):+.3f}%/일 "
        f"-> 비용 후 {statistics.fmean(lvo) - COST:+.3f}%/일"
    )
    print("B2. N일 보유는 선정 없는 유니버스 보유 = 베타 — 전략이 아니다 (docstring 참조).")

    over_symdays.sort()
    n = len(over_symdays)
    print()
    print("[갭 리스크] 오버나이트 분포 (종목-일):")
    print(
        f"  표본 {n:,} · 1% 분위 {over_symdays[int(0.01 * n)]:+.2f}% · "
        f"5% 분위 {over_symdays[int(0.05 * n)]:+.2f}% · 최악 {over_symdays[0]:+.2f}%"
    )
    print("  -> 오버나이트를 먹으려면 이 꼬리를 손절 없이 통째로 떠안는다 (장전엔 팔 수 없음).")


if __name__ == "__main__":
    main()
