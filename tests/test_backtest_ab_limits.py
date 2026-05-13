"""백테스트 A/B 비교 — 29단계 PR41 (#8)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from auto_trading.backtest.ab_limits import ABResult, ab_compare_backtests
from auto_trading.strategy.base import Bar, Side, Signal, Strategy


class _AlwaysBuyOnce(Strategy):
    def __init__(self) -> None:
        super().__init__()
        self._done = False

    def on_bar(self, bar: Bar) -> list[Signal]:
        if not self._done:
            self._done = True
            return [Signal(symbol=bar.symbol, side=Side.BUY, weight=1.0)]
        return []

    def reset(self) -> None:
        super().reset()
        self._done = False


def _factory() -> Strategy:
    return _AlwaysBuyOnce()


def _df_with_low_volume(n: int = 20) -> pd.DataFrame:
    rows = [
        {
            "ts": datetime(2026, 1, 1) + timedelta(days=i),
            "open": 10_000.0,
            "high": 10_100.0,
            "low": 9_900.0,
            "close": 10_000.0 * (1 + 0.005 * i),
            "volume": 100,
        }
        for i in range(n)
    ]
    return pd.DataFrame(rows).set_index("ts")


def test_ab_compare_returns_result_with_two_reports() -> None:
    df = _df_with_low_volume()
    result = ab_compare_backtests(
        _factory,
        "X",
        df,
        variant_a={"slippage_bps": 5.0, "slippage_model": "fixed"},
        variant_b={"slippage_bps": 5.0, "slippage_model": "fixed"},
    )
    assert isinstance(result, ABResult)
    assert result.label_a == "conservative"
    assert result.label_b == "aggressive"
    # 동일 설정 → 동일 결과 → diff 모두 0.
    for d in result.diffs:
        assert d.delta == 0.0


def test_ab_compare_detects_pnl_difference_between_slippage_models() -> None:
    """fixed vs impact 두 변형 → 체결 가격·PnL 차이 → diff != 0."""
    df = _df_with_low_volume(20)
    result = ab_compare_backtests(
        _factory,
        "X",
        df,
        variant_a={"slippage_bps": 5.0, "slippage_model": "fixed"},
        variant_b={
            "slippage_bps": 5.0,
            "slippage_model": "impact",
            "impact_coefficient": 3.0,
        },
        label_a="fixed",
        label_b="impact",
    )
    # 최소 한 메트릭은 차이 발생 (final_equity 등).
    non_zero = [d for d in result.diffs if d.delta != 0.0]
    assert non_zero, "impact 모델은 fixed 와 비교해 최소 1개 메트릭 차이가 있어야 함"


def test_ab_compare_diffs_use_compare_reports_format() -> None:
    """PR32 의 MetricDiff 구조를 그대로 사용 (name·old·new·delta·direction·regression)."""
    df = _df_with_low_volume()
    result = ab_compare_backtests(
        _factory,
        "X",
        df,
        variant_a={},
        variant_b={"slippage_bps": 10.0},
    )
    for d in result.diffs:
        assert hasattr(d, "name")
        assert hasattr(d, "old")
        assert hasattr(d, "new")
        assert hasattr(d, "delta")
        assert hasattr(d, "direction")
        assert hasattr(d, "regression")


def test_ab_compare_strategy_kwarg_in_variant_is_ignored() -> None:
    """variant kwargs 에 'strategy' 가 들어와도 factory 가 생성한 인스턴스가 우선."""
    df = _df_with_low_volume()
    # strategy 를 일부러 잘못된 값으로 넣어도 factory 인스턴스가 사용되어 정상 동작.
    result = ab_compare_backtests(
        _factory,
        "X",
        df,
        variant_a={"strategy": "ignored", "slippage_bps": 5.0},
        variant_b={"strategy": object(), "slippage_bps": 10.0},
    )
    assert result.report_a.num_trades >= 1
    assert result.report_b.num_trades >= 1


def test_ab_compare_label_propagation() -> None:
    df = _df_with_low_volume()
    result = ab_compare_backtests(
        _factory,
        "X",
        df,
        variant_a={},
        variant_b={},
        label_a="baseline",
        label_b="proposed",
    )
    assert result.label_a == "baseline"
    assert result.label_b == "proposed"


def test_ab_compare_deterministic_for_same_input() -> None:
    df = _df_with_low_volume()
    r1 = ab_compare_backtests(_factory, "X", df, variant_a={}, variant_b={"slippage_bps": 10.0})
    r2 = ab_compare_backtests(_factory, "X", df, variant_a={}, variant_b={"slippage_bps": 10.0})
    assert r1.report_a.sharpe == r2.report_a.sharpe
    assert r1.report_b.sharpe == r2.report_b.sharpe
