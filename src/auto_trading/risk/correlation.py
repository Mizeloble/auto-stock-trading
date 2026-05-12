"""보유 포지션 간 페어 상관계수 — 29단계 PR27 (#15 Phase 1).

무엇/왜:
- 같은 섹터 5종목 = 사실상 1종목. ``max_position_weight`` 기준으론 분산돼 보이지만
  상관계수 0.7+ 이면 코스피 폭락 시 동시 손절 — 분산 효과 0.
- 신규 BUY 후보가 기존 보유와 임계(권장 0.7) 이상 양의 상관이면 risk manager 가 REJECT.

이번 PR 의 책임:
- *순수 계산* (수익률 시계열 → 페어 상관계수 dict)
- *판정 헬퍼* (특정 종목이 보유와 갖는 최대 양의 상관)

risk/ 규칙: 외부 호출 금지 — 데이터 공급(``market.chart.get_daily_bars`` 로 30일 일별 종가
→ 일별 수익률 변환) 은 호출자(러너) 책임. 이번 PR 에선 러너 통합은 안 하고 다음 PR
로 분리 (작은 PR 원칙).

설계 결정:
- 음의 상관은 분산 효과 — 가드 관심 대상 X. 양의 상관만 임계 비교.
- 페어 키는 항상 정렬된 튜플 (s_min, s_max) — 룩업 모호성 방지.
- 표본 < 2 또는 분산 0 인 페어는 결과에서 제외 (corrcoef NaN 회피).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from itertools import combinations, pairwise

import numpy as np

__all__ = [
    "daily_returns",
    "max_correlation_with_holdings",
    "pairwise_pearson_correlation",
]


def daily_returns(prices: list[float]) -> list[float]:
    """일별 종가 시계열 → 일별 수익률 (close[i] / close[i-1] - 1).

    prev <= 0 인 페어는 스킵 (배당락/액면분할 데이터 누락 보호).
    """
    if len(prices) < 2:
        return []
    returns: list[float] = []
    for prev, curr in pairwise(prices):
        if prev <= 0:
            continue
        returns.append(curr / prev - 1.0)
    return returns


def pairwise_pearson_correlation(
    returns: Mapping[str, list[float]],
) -> dict[tuple[str, str], float]:
    """일별 수익률 dict → 페어 Pearson 상관계수 dict.

    키는 정렬된 튜플 (s_min, s_max). 표본 부족 또는 분산 0 페어는 결과에서 제외.

    어떻게 확인: tests/test_risk_correlation.py 의 동일 시계열 페어 → 1.0,
    반대 시계열 페어 → -1.0 케이스로 검증.
    """
    result: dict[tuple[str, str], float] = {}
    items = sorted(returns.items())
    for (s1, r1), (s2, r2) in combinations(items, 2):
        n = min(len(r1), len(r2))
        if n < 2:
            continue
        a = np.asarray(r1[-n:], dtype=float)
        b = np.asarray(r2[-n:], dtype=float)
        # 분산 0 (값 전부 동일) 면 numpy.corrcoef NaN → 제외.
        if float(a.std()) == 0.0 or float(b.std()) == 0.0:
            continue
        c = float(np.corrcoef(a, b)[0, 1])
        if np.isnan(c):
            continue
        result[(s1, s2)] = c
    return result


def max_correlation_with_holdings(
    target: str,
    holdings: Iterable[str],
    correlations: Mapping[tuple[str, str], float],
) -> tuple[str, float] | None:
    """target 종목이 holdings 중 어느 보유와 가장 양의 상관이 높은지.

    무엇/왜:
    - 음의 상관은 분산 효과이므로 가드 관심 대상 X. 양의 상관만 후보.
    - target ∈ holdings 인 자기 자신은 제외 (의미 없음).
    - 양의 상관 페어가 없으면 None — 호출자는 "임계 검사 통과" 로 해석.

    반환: ``(보유 종목 코드, 상관계수)`` 또는 None.
    """
    best: tuple[str, float] | None = None
    for h in holdings:
        if h == target:
            continue
        key = (target, h) if target < h else (h, target)
        c = correlations.get(key)
        if c is None or c <= 0:
            continue
        if best is None or c > best[1]:
            best = (h, c)
    return best
