"""ATR 기반 weight 정규화 — 29단계 PR39 (#6 risk.manager 결합).

backtest 의 atr_normalized sizing 과 같은 직관: 변동성 큰 종목엔 작은 weight,
작은 종목엔 큰 weight (0~1 clamp). 호출자가 ATR 데이터 주입.
"""

from __future__ import annotations

from auto_trading.risk.manager import (
    AccountState,
    Decision,
    RiskLimits,
    RiskManager,
)
from auto_trading.strategy.base import Side, Signal


def _buy(symbol: str = "AAA", weight: float = 1.0) -> Signal:
    return Signal(symbol=symbol, side=Side.BUY, weight=weight)


def _account() -> AccountState:
    return AccountState(
        deposit=10_000_000,
        total_equity=10_000_000,
        day_start_equity=10_000_000,
    )


def test_atr_weight_disabled_by_default_preserves_behavior() -> None:
    """atr_normalized_weight=False (기본) → atr_per_symbol 무시. 기존 결과 동일."""
    rm_default = RiskManager(limits=RiskLimits(max_position_weight=0.2, max_portfolio_exposure=1.0))
    rm_explicit = RiskManager(
        limits=RiskLimits(
            max_position_weight=0.2,
            max_portfolio_exposure=1.0,
            atr_normalized_weight=False,
        )
    )
    d1 = rm_default.evaluate(_buy(), price=10_000, account=_account())
    d2 = rm_explicit.evaluate(
        _buy(), price=10_000, account=_account(), atr_per_symbol={"AAA": 500.0}
    )
    assert d1.decision is Decision.APPROVE
    assert d2.decision is Decision.APPROVE
    assert d1.order is not None and d2.order is not None
    assert d1.order.quantity == d2.order.quantity


def test_atr_weight_reduces_qty_when_atr_pct_above_target() -> None:
    """ATR/price = 2% > target 1% → weight x 0.5 → 수량 절반."""
    rm = RiskManager(
        limits=RiskLimits(
            max_position_weight=1.0,
            max_portfolio_exposure=1.0,
            atr_normalized_weight=True,
            atr_target_risk_pct=0.01,
        )
    )
    # baseline 수량 — atr 데이터 없을 때.
    d_no_atr = rm.evaluate(_buy(), price=10_000, account=_account())
    # atr 비율 2% → weight 절반.
    d_atr = rm.evaluate(
        _buy(),
        price=10_000,
        account=_account(),
        atr_per_symbol={"AAA": 200.0},  # 2% of price
    )
    assert d_no_atr.order is not None and d_atr.order is not None
    # 수량은 weight 에 비례 — 절반에 가깝지만 Decimal 반올림으로 정확 절반 보장 안 됨.
    assert d_atr.order.quantity < d_no_atr.order.quantity
    assert d_atr.order.quantity <= d_no_atr.order.quantity // 2 + 1


def test_atr_weight_does_not_exceed_one_for_low_volatility() -> None:
    """ATR 0.5% < target 1% → ratio=2 가지만 weight clamp 1.0 → 수량 변동 X."""
    rm = RiskManager(
        limits=RiskLimits(
            max_position_weight=1.0,
            max_portfolio_exposure=1.0,
            atr_normalized_weight=True,
            atr_target_risk_pct=0.01,
        )
    )
    d_no_atr = rm.evaluate(_buy(), price=10_000, account=_account())
    d_atr = rm.evaluate(
        _buy(),
        price=10_000,
        account=_account(),
        atr_per_symbol={"AAA": 50.0},  # 0.5% — clamp 적용
    )
    assert d_no_atr.order is not None and d_atr.order is not None
    assert d_atr.order.quantity == d_no_atr.order.quantity


def test_atr_weight_missing_symbol_falls_back() -> None:
    """atr_per_symbol 에 해당 종목 없으면 정규화 skip — 기본 weight."""
    rm = RiskManager(
        limits=RiskLimits(
            max_position_weight=1.0,
            max_portfolio_exposure=1.0,
            atr_normalized_weight=True,
            atr_target_risk_pct=0.01,
        )
    )
    d = rm.evaluate(
        _buy("BBB"),
        price=10_000,
        account=_account(),
        atr_per_symbol={"AAA": 200.0},  # BBB 누락
    )
    d_no_atr = rm.evaluate(_buy("BBB"), price=10_000, account=_account())
    assert d.order is not None and d_no_atr.order is not None
    assert d.order.quantity == d_no_atr.order.quantity


def test_atr_weight_zero_or_negative_atr_skipped() -> None:
    """ATR=0 또는 음수면 정규화 skip (분모 0 방지)."""
    rm = RiskManager(
        limits=RiskLimits(
            max_position_weight=1.0,
            max_portfolio_exposure=1.0,
            atr_normalized_weight=True,
            atr_target_risk_pct=0.01,
        )
    )
    d_no_atr = rm.evaluate(_buy(), price=10_000, account=_account())
    d_zero = rm.evaluate(_buy(), price=10_000, account=_account(), atr_per_symbol={"AAA": 0.0})
    d_neg = rm.evaluate(_buy(), price=10_000, account=_account(), atr_per_symbol={"AAA": -10.0})
    assert d_zero.order is not None and d_neg.order is not None and d_no_atr.order is not None
    assert d_zero.order.quantity == d_no_atr.order.quantity
    assert d_neg.order.quantity == d_no_atr.order.quantity


def test_atr_weight_does_not_affect_sell() -> None:
    """SELL 은 보유 비례라 ATR 정규화 의미 X — 무시."""
    rm = RiskManager(
        limits=RiskLimits(
            atr_normalized_weight=True,
            atr_target_risk_pct=0.01,
        )
    )
    acct = AccountState(
        deposit=0,
        total_equity=1_000_000,
        positions={"AAA": 10},
        avg_prices={"AAA": 10_000},
    )
    d = rm.evaluate(
        Signal(symbol="AAA", side=Side.SELL, weight=1.0),
        price=10_500,
        account=acct,
        atr_per_symbol={"AAA": 500.0},  # 5% — ratio 0.2
    )
    assert d.decision is Decision.APPROVE
    assert d.order is not None
    assert d.order.quantity == 10  # 전량 매도 (weight 정규화 영향 X)
