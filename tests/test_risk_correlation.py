"""risk.correlation 회귀 — 29단계 PR27 (#15 Phase 1).

- daily_returns: 종가 → 일별 수익률 (음수/0 prev 보호)
- pairwise_pearson_correlation: 페어 dict, 정렬 키, NaN/표본 부족 제외
- max_correlation_with_holdings: 양의 상관 최댓값, 자기 자신 제외
"""

from __future__ import annotations

import pytest

from auto_trading.risk.correlation import (
    daily_returns,
    max_correlation_with_holdings,
    pairwise_pearson_correlation,
)

# ---- daily_returns -----------------------------------------------------------


def test_daily_returns_basic() -> None:
    out = daily_returns([100.0, 110.0, 99.0])
    assert out == pytest.approx([0.10, -0.10])


def test_daily_returns_empty_when_under_two_points() -> None:
    assert daily_returns([]) == []
    assert daily_returns([100.0]) == []


def test_daily_returns_skips_nonpositive_prev() -> None:
    """배당락 등 prev=0 데이터 누락 → 해당 페어 스킵."""
    out = daily_returns([100.0, 0.0, 200.0, 220.0])
    # (100→0) 은 무한대 손실로 의미 X — daily_returns 는 prev=0 만 스킵, 0→200 은 계산.
    # 100→0 페어는 prev>0 이라 -1.0 결과.
    # 0→200 페어는 prev=0 → 스킵.
    # 200→220 페어는 정상 → 0.10.
    assert out == pytest.approx([-1.0, 0.10])


# ---- pairwise_pearson_correlation -------------------------------------------


def test_pairwise_identical_series_gives_one() -> None:
    returns = {
        "AAA": [0.01, -0.02, 0.03, -0.01],
        "BBB": [0.01, -0.02, 0.03, -0.01],
    }
    corr = pairwise_pearson_correlation(returns)
    assert corr[("AAA", "BBB")] == pytest.approx(1.0)


def test_pairwise_opposite_series_gives_negative_one() -> None:
    returns = {
        "AAA": [0.01, -0.02, 0.03, -0.01],
        "BBB": [-0.01, 0.02, -0.03, 0.01],
    }
    corr = pairwise_pearson_correlation(returns)
    assert corr[("AAA", "BBB")] == pytest.approx(-1.0)


def test_pairwise_keys_are_sorted_tuples() -> None:
    """룩업 모호성 방지 — (s_min, s_max) 정렬."""
    returns = {
        "ZED": [0.01, 0.02, 0.03, 0.04],
        "ABC": [0.04, 0.03, 0.02, 0.01],
    }
    corr = pairwise_pearson_correlation(returns)
    assert ("ABC", "ZED") in corr
    assert ("ZED", "ABC") not in corr


def test_pairwise_excludes_zero_variance() -> None:
    """값이 전부 동일 (분산 0) 한 페어는 NaN — 결과에서 제외."""
    returns = {
        "FLAT": [0.0, 0.0, 0.0, 0.0],
        "MOVE": [0.01, -0.02, 0.03, -0.01],
    }
    corr = pairwise_pearson_correlation(returns)
    assert ("FLAT", "MOVE") not in corr


def test_pairwise_excludes_too_short() -> None:
    returns = {
        "AAA": [0.01],
        "BBB": [0.02],
    }
    assert pairwise_pearson_correlation(returns) == {}


def test_pairwise_uses_overlap_tail_when_unequal_length() -> None:
    """길이 다른 페어는 짧은 쪽 길이만큼 *끝* 에서 정렬해 사용."""
    returns = {
        "AAA": [-99.0, 0.01, 0.02, 0.03],  # 앞부분 -99 무시되어야
        "BBB": [0.01, 0.02, 0.03],
    }
    corr = pairwise_pearson_correlation(returns)
    # 뒤 3개 (0.01, 0.02, 0.03) 끼리 비교 → 완전 동일 → 1.0.
    assert corr[("AAA", "BBB")] == pytest.approx(1.0)


# ---- max_correlation_with_holdings ------------------------------------------


def test_max_correlation_picks_strongest_positive() -> None:
    corr = {
        ("AAA", "TGT"): 0.5,
        ("BBB", "TGT"): 0.9,  # 가장 강한 양의 상관
        ("CCC", "TGT"): 0.3,
    }
    best = max_correlation_with_holdings("TGT", ["AAA", "BBB", "CCC"], corr)
    assert best == ("BBB", pytest.approx(0.9))


def test_max_correlation_skips_self() -> None:
    """target ∈ holdings 면 자기 자신은 무시."""
    corr = {("AAA", "TGT"): 0.5}
    best = max_correlation_with_holdings("TGT", ["TGT", "AAA"], corr)
    assert best == ("AAA", pytest.approx(0.5))


def test_max_correlation_ignores_negative() -> None:
    """음의 상관은 분산 효과 — 가드 관심 X."""
    corr = {
        ("AAA", "TGT"): -0.95,  # 강한 음의 상관 — 무시되어야
        ("BBB", "TGT"): 0.2,  # 약한 양의 상관 — 선택
    }
    best = max_correlation_with_holdings("TGT", ["AAA", "BBB"], corr)
    assert best == ("BBB", pytest.approx(0.2))


def test_max_correlation_returns_none_when_no_positive_pairs() -> None:
    corr: dict[tuple[str, str], float] = {("AAA", "TGT"): -0.5}
    best = max_correlation_with_holdings("TGT", ["AAA"], corr)
    assert best is None


def test_max_correlation_returns_none_when_holdings_empty() -> None:
    best = max_correlation_with_holdings("TGT", [], {})
    assert best is None


def test_max_correlation_missing_pair_skipped() -> None:
    """correlations 에 없는 페어는 스킵 (표본 부족으로 계산 안 된 경우)."""
    corr = {("AAA", "TGT"): 0.85}
    best = max_correlation_with_holdings("TGT", ["AAA", "MISSING"], corr)
    assert best == ("AAA", pytest.approx(0.85))
