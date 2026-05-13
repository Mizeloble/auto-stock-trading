"""거래량 기반 슬리피지 모델 — 29단계 PR35 (#4).

fixed 모델 회귀 0 + impact 모델 산식 정확성 + bar_volume=0 fallback.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from auto_trading.backtest.broker import SimulatedBroker
from auto_trading.backtest.engine import Backtester
from auto_trading.strategy.base import Bar, Side, Signal, Strategy


def _ts() -> datetime:
    return datetime(2026, 5, 1)


# ---- _effective_slippage_bps -----------------------------------------------


def test_fixed_model_ignores_volume() -> None:
    """slippage_model='fixed' → bar_volume 영향 X."""
    broker = SimulatedBroker(cash=1_000_000, slippage_bps=5.0, slippage_model="fixed")
    bps_small = broker._effective_slippage_bps(quantity=10, bar_volume=10_000)
    bps_large = broker._effective_slippage_bps(quantity=10_000, bar_volume=10_000)
    assert bps_small == 5.0
    assert bps_large == 5.0


def test_impact_model_adds_bps_proportional_to_ratio() -> None:
    """비율 1% → +impact_coefficient bp. 5% → +5*impact_coefficient bp."""
    broker = SimulatedBroker(
        cash=1_000_000,
        slippage_bps=5.0,
        slippage_model="impact",
        impact_coefficient=2.0,
    )
    # 100/10_000 = 1% → 5 + 2*1 = 7
    bps_1pct = broker._effective_slippage_bps(quantity=100, bar_volume=10_000)
    assert bps_1pct == 7.0
    # 500/10_000 = 5% → 5 + 2*5 = 15
    bps_5pct = broker._effective_slippage_bps(quantity=500, bar_volume=10_000)
    assert bps_5pct == 15.0


def test_impact_model_falls_back_to_fixed_when_volume_zero() -> None:
    """bar_volume=0 (데이터 누락) → fixed slippage_bps 그대로."""
    broker = SimulatedBroker(cash=1_000_000, slippage_bps=5.0, slippage_model="impact")
    bps = broker._effective_slippage_bps(quantity=1_000, bar_volume=0)
    assert bps == 5.0


def test_impact_model_falls_back_when_quantity_zero() -> None:
    broker = SimulatedBroker(cash=1_000_000, slippage_bps=5.0, slippage_model="impact")
    bps = broker._effective_slippage_bps(quantity=0, bar_volume=10_000)
    assert bps == 5.0


# ---- execute() 통합 --------------------------------------------------------


def test_execute_fixed_model_price_unchanged_by_volume() -> None:
    """fixed 모델 — 같은 수량, 거래량 다름 → 동일 체결가."""
    broker_a = SimulatedBroker(cash=10_000_000, slippage_bps=5.0, slippage_model="fixed")
    broker_b = SimulatedBroker(cash=10_000_000, slippage_bps=5.0, slippage_model="fixed")
    t_a = broker_a.execute(
        symbol="X", side=Side.BUY, quantity=100, bar_open=10_000, bar_volume=100, ts=_ts()
    )
    t_b = broker_b.execute(
        symbol="X", side=Side.BUY, quantity=100, bar_open=10_000, bar_volume=1_000_000, ts=_ts()
    )
    assert t_a is not None and t_b is not None
    assert t_a.price == t_b.price


def test_execute_impact_model_price_changes_with_volume() -> None:
    """impact 모델 — 같은 수량, 거래량 작을수록 BUY 가격 ↑ (불리)."""
    broker_low = SimulatedBroker(
        cash=10_000_000, slippage_bps=5.0, slippage_model="impact", impact_coefficient=2.0
    )
    broker_high = SimulatedBroker(
        cash=10_000_000, slippage_bps=5.0, slippage_model="impact", impact_coefficient=2.0
    )
    t_low_vol = broker_low.execute(
        symbol="X", side=Side.BUY, quantity=100, bar_open=10_000, bar_volume=1_000, ts=_ts()
    )
    t_high_vol = broker_high.execute(
        symbol="X", side=Side.BUY, quantity=100, bar_open=10_000, bar_volume=1_000_000, ts=_ts()
    )
    assert t_low_vol is not None and t_high_vol is not None
    # 거래량 작은 쪽이 더 비싸게 체결.
    assert t_low_vol.price > t_high_vol.price


def test_execute_impact_sell_price_lower_with_low_volume() -> None:
    """impact 모델 — SELL 도 거래량 작을수록 *낮은* 가격에 체결 (불리)."""
    from auto_trading.backtest.broker import Position

    broker_low = SimulatedBroker(
        cash=0, slippage_bps=5.0, slippage_model="impact", impact_coefficient=2.0
    )
    broker_high = SimulatedBroker(
        cash=0, slippage_bps=5.0, slippage_model="impact", impact_coefficient=2.0
    )
    broker_low.positions["X"] = Position(quantity=1_000, avg_price=10_000)
    broker_high.positions["X"] = Position(quantity=1_000, avg_price=10_000)
    t_low_vol = broker_low.execute(
        symbol="X", side=Side.SELL, quantity=100, bar_open=10_000, bar_volume=1_000, ts=_ts()
    )
    t_high_vol = broker_high.execute(
        symbol="X", side=Side.SELL, quantity=100, bar_open=10_000, bar_volume=1_000_000, ts=_ts()
    )
    assert t_low_vol is not None and t_high_vol is not None
    assert t_low_vol.price < t_high_vol.price


# ---- 백테스트 엔진 통합 -----------------------------------------------------


class _AlwaysBuy(Strategy):
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


def _df_with_low_volume() -> pd.DataFrame:
    rows = [
        {
            "ts": datetime(2026, 1, 1) + timedelta(days=i),
            "open": 10_000.0,
            "high": 10_100.0,
            "low": 9_900.0,
            "close": 10_000.0,
            "volume": 100,  # 매우 작은 거래량 — 매수 폭주 효과
        }
        for i in range(5)
    ]
    return pd.DataFrame(rows).set_index("ts")


def test_backtester_impact_model_results_differ_from_fixed() -> None:
    """동일 데이터·전략에 fixed vs impact → 체결 가격 + 최종 자본 다름.

    소형주(낮은 거래량)에서 백테스트와 실거래 PnL 괴리 시뮬.
    """
    df = _df_with_low_volume()

    bt_fixed = Backtester(
        strategy=_AlwaysBuy(),
        initial_cash=1_000_000,
        slippage_model="fixed",
        slippage_bps=5.0,
    )
    r_fixed = bt_fixed.run("X", df)

    bt_impact = Backtester(
        strategy=_AlwaysBuy(),
        initial_cash=1_000_000,
        slippage_model="impact",
        slippage_bps=5.0,
        impact_coefficient=2.0,
    )
    r_impact = bt_impact.run("X", df)

    assert len(r_fixed.trades) >= 1
    assert len(r_impact.trades) >= 1
    # impact 모델이 거래량 작은 봉에서 더 비싸게 체결.
    assert r_impact.trades[0].price > r_fixed.trades[0].price


def test_backtester_default_fixed_model_preserves_legacy_behavior() -> None:
    """기본은 fixed — 기존 백테스트 결과 회귀 0 (호환 보존)."""
    df = _df_with_low_volume()
    bt_default = Backtester(strategy=_AlwaysBuy(), initial_cash=1_000_000, slippage_bps=5.0)
    bt_explicit = Backtester(
        strategy=_AlwaysBuy(),
        initial_cash=1_000_000,
        slippage_bps=5.0,
        slippage_model="fixed",
    )
    r1 = bt_default.run("X", df)
    r2 = bt_explicit.run("X", df)
    assert len(r1.trades) == len(r2.trades)
    for a, b in zip(r1.trades, r2.trades, strict=True):
        assert a.price == b.price
        assert a.quantity == b.quantity
