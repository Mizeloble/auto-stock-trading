"""backtest.compare — 29단계 PR32 (#23).

각 방향(higher/lower/less_negative/info_only) 회귀 판정 정확성 + 키 누락 / 추가.
"""

from __future__ import annotations

import pytest

from auto_trading.backtest.compare import Direction, compare_reports


def _by_name(diffs, name):
    return next(d for d in diffs if d.name == name)


def test_higher_is_better_detects_regression() -> None:
    """Sharpe 떨어지면 회귀."""
    diffs = compare_reports({"sharpe": 1.2}, {"sharpe": 1.0})
    d = _by_name(diffs, "sharpe")
    assert d.direction is Direction.HIGHER_IS_BETTER
    assert d.delta == pytest.approx(-0.2)
    assert d.regression is True


def test_higher_is_better_detects_improvement() -> None:
    """Sharpe 오르면 회귀 X."""
    diffs = compare_reports({"sharpe": 1.0}, {"sharpe": 1.2})
    assert _by_name(diffs, "sharpe").regression is False


def test_less_negative_is_better_detects_deeper_drawdown() -> None:
    """MDD -5% → -10% 는 회귀 (더 깊은 손실)."""
    diffs = compare_reports({"max_drawdown_pct": -5.0}, {"max_drawdown_pct": -10.0})
    d = _by_name(diffs, "max_drawdown_pct")
    assert d.direction is Direction.LESS_NEGATIVE_IS_BETTER
    assert d.regression is True


def test_less_negative_is_better_detects_shallower_drawdown() -> None:
    """MDD -10% → -5% 는 개선."""
    diffs = compare_reports({"max_drawdown_pct": -10.0}, {"max_drawdown_pct": -5.0})
    assert _by_name(diffs, "max_drawdown_pct").regression is False


def test_lower_is_better_detects_higher_volatility() -> None:
    """변동성 상승은 회귀."""
    diffs = compare_reports({"annualized_vol_pct": 10.0}, {"annualized_vol_pct": 15.0})
    d = _by_name(diffs, "annualized_vol_pct")
    assert d.direction is Direction.LOWER_IS_BETTER
    assert d.regression is True


def test_info_only_never_regression() -> None:
    """num_trades 가 줄어도 회귀 판정 X."""
    diffs = compare_reports({"num_trades": 100}, {"num_trades": 50})
    d = _by_name(diffs, "num_trades")
    assert d.direction is Direction.INFO_ONLY
    assert d.regression is False


def test_equal_values_not_regression() -> None:
    """동일 값은 회귀 X (어느 방향이든)."""
    diffs = compare_reports({"sharpe": 1.0}, {"sharpe": 1.0})
    assert _by_name(diffs, "sharpe").regression is False


def test_probabilistic_sharpe_drop_detects_regression() -> None:
    """PR31 신규 메트릭 — PSR 떨어지면 회귀."""
    diffs = compare_reports({"probabilistic_sharpe": 0.9}, {"probabilistic_sharpe": 0.7})
    d = _by_name(diffs, "probabilistic_sharpe")
    assert d.regression is True


def test_missing_key_in_one_side_treated_as_zero() -> None:
    """old 에만 있는 키 — new 0 으로 비교 (값 사라짐 = 회귀)."""
    diffs = compare_reports({"sharpe": 1.2}, {})
    d = _by_name(diffs, "sharpe")
    assert d.new == 0.0
    assert d.regression is True


def test_unknown_key_classified_as_info_only() -> None:
    """미래 신규 메트릭이 양쪽에 있으면 info_only 로 표시 (안전 default)."""
    diffs = compare_reports({"future_metric": 1.0}, {"future_metric": 2.0})
    d = _by_name(diffs, "future_metric")
    assert d.direction is Direction.INFO_ONLY
    assert d.regression is False


def test_known_keys_preserve_declared_order() -> None:
    """출력 순서가 _DIRECTION 선언 순서를 따른다 (사용자가 표 읽기 좋게)."""
    diffs = compare_reports(
        {
            "num_trades": 10,
            "sharpe": 1.0,
            "max_drawdown_pct": -3.0,
        },
        {
            "num_trades": 12,
            "sharpe": 1.1,
            "max_drawdown_pct": -2.5,
        },
    )
    names = [d.name for d in diffs]
    # sharpe 가 max_drawdown_pct 보다 먼저, num_trades 보다 먼저.
    assert names.index("sharpe") < names.index("max_drawdown_pct")
    assert names.index("max_drawdown_pct") < names.index("num_trades")


def test_non_numeric_unknown_key_skipped() -> None:
    """미래 메트릭이 문자열 등 비숫자면 안전하게 skip."""
    diffs = compare_reports(
        {"sharpe": 1.0, "stage": "alpha"},
        {"sharpe": 1.1, "stage": "beta"},
    )
    names = [d.name for d in diffs]
    assert "stage" not in names
    assert "sharpe" in names
