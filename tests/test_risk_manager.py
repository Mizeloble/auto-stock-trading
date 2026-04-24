"""RiskManager 검증."""

from __future__ import annotations

from auto_trading.account.orders import Side as OrderSide
from auto_trading.risk.manager import AccountState, Decision, RiskLimits, RiskManager
from auto_trading.strategy.base import Side as SignalSide
from auto_trading.strategy.base import Signal


def _buy(symbol: str = "005930", weight: float = 1.0) -> Signal:
    return Signal(symbol=symbol, side=SignalSide.BUY, weight=weight)


def _sell(symbol: str = "005930", weight: float = 1.0) -> Signal:
    return Signal(symbol=symbol, side=SignalSide.SELL, weight=weight)


def test_buy_respects_max_position_weight() -> None:
    rm = RiskManager(limits=RiskLimits(max_position_weight=0.2, max_portfolio_exposure=1.0))
    acct = AccountState(deposit=10_000_000, total_equity=10_000_000, day_start_equity=10_000_000)

    d = rm.evaluate(_buy(), price=70_000, account=acct)

    assert d.decision is Decision.APPROVE
    assert d.order is not None
    assert d.order.side is OrderSide.BUY
    # 10,000,000 * 20% = 2,000,000 → 2,000,000 // 70,000 = 28주
    assert d.order.quantity == 28


def test_buy_rejected_when_daily_loss_exceeded() -> None:
    rm = RiskManager(limits=RiskLimits(daily_loss_limit=0.03))
    acct = AccountState(deposit=10_000_000, total_equity=9_600_000, day_start_equity=10_000_000)

    d = rm.evaluate(_buy(), price=70_000, account=acct)
    assert d.decision is Decision.REJECT
    assert "daily loss" in d.reason


def test_buy_rejected_when_budget_below_minimum() -> None:
    rm = RiskManager(limits=RiskLimits(min_order_value=50_000))
    acct = AccountState(deposit=5_000, total_equity=5_000, day_start_equity=5_000)
    d = rm.evaluate(_buy(), price=1_000, account=acct)
    assert d.decision is Decision.REJECT


def test_sell_without_position_rejected() -> None:
    rm = RiskManager()
    acct = AccountState(deposit=10_000_000, total_equity=10_000_000)
    d = rm.evaluate(_sell(), price=70_000, account=acct)
    assert d.decision is Decision.REJECT


def test_sell_with_position_approves_partial_or_full() -> None:
    rm = RiskManager()
    acct = AccountState(
        deposit=0,
        total_equity=700_000,
        positions={"005930": 10},
        avg_prices={"005930": 70_000},
    )
    d = rm.evaluate(_sell(weight=1.0), price=71_000, account=acct)
    assert d.decision is Decision.APPROVE
    assert d.order is not None
    assert d.order.side is OrderSide.SELL
    assert d.order.quantity == 10


def test_existing_position_reduces_remaining_budget() -> None:
    rm = RiskManager(limits=RiskLimits(max_position_weight=0.2, max_portfolio_exposure=1.0))
    acct = AccountState(
        deposit=10_000_000,
        total_equity=10_000_000,
        positions={"005930": 20},
        avg_prices={"005930": 70_000},
    )
    # 이미 보유 1,400,000 → 남은 한도 600,000 → 600,000 // 70,000 = 8주
    d = rm.evaluate(_buy(), price=70_000, account=acct)
    assert d.decision is Decision.APPROVE
    assert d.order is not None and d.order.quantity == 8


def test_sizing_uses_decimal_at_rounding_boundary() -> None:
    """float 산술이었다면 budget=1_400_000.00000001 → qty 한 주 더 → 오버런."""
    rm = RiskManager(limits=RiskLimits(max_position_weight=0.14, max_portfolio_exposure=1.0))
    acct = AccountState(deposit=10_000_000, total_equity=10_000_000, day_start_equity=10_000_000)

    d = rm.evaluate(_buy(), price=100_000, account=acct)
    # 10_000_000 * 0.14 = 1_400_000 정확 → 14주
    assert d.decision is Decision.APPROVE
    assert d.order is not None and d.order.quantity == 14
