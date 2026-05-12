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


# ---- 23단계 Phase B · 1회/일일 절대금액 한도 + kill switch -------------------


def test_max_order_value_caps_buy_budget() -> None:
    """max_order_value=500,000 이면 비중 한도 2,000,000 보다 작아 capping."""
    rm = RiskManager(
        limits=RiskLimits(
            max_position_weight=0.2,
            max_portfolio_exposure=1.0,
            max_order_value=500_000,
        )
    )
    acct = AccountState(deposit=10_000_000, total_equity=10_000_000, day_start_equity=10_000_000)
    d = rm.evaluate(_buy(), price=70_000, account=acct)
    assert d.decision is Decision.APPROVE
    assert d.order is not None
    # 500,000 // 70,000 = 7주 (28주 → 7주로 capping)
    assert d.order.quantity == 7


def test_max_order_value_zero_means_unlimited() -> None:
    """기본값 0 은 비활성 — 비중 한도가 그대로 적용."""
    rm = RiskManager(limits=RiskLimits(max_position_weight=0.2, max_portfolio_exposure=1.0))
    acct = AccountState(deposit=10_000_000, total_equity=10_000_000, day_start_equity=10_000_000)
    d = rm.evaluate(_buy(), price=70_000, account=acct)
    assert d.decision is Decision.APPROVE
    assert d.order is not None and d.order.quantity == 28


def test_daily_notional_limit_caps_remaining_budget() -> None:
    """일일 한도 1,500,000 + 이미 1,000,000 사용 → 남은 500,000 까지만."""
    rm = RiskManager(
        limits=RiskLimits(
            max_position_weight=0.2,
            max_portfolio_exposure=1.0,
            daily_notional_limit=1_500_000,
        )
    )
    acct = AccountState(
        deposit=10_000_000,
        total_equity=10_000_000,
        day_start_equity=10_000_000,
        today_notional_used=1_000_000,
    )
    d = rm.evaluate(_buy(), price=70_000, account=acct)
    assert d.decision is Decision.APPROVE
    assert d.order is not None
    # 남은 500,000 // 70,000 = 7주 (전체 한도 28주 → 7주)
    assert d.order.quantity == 7


def test_daily_notional_limit_rejects_when_exhausted() -> None:
    """일일 한도 도달이면 BUY 거절."""
    rm = RiskManager(limits=RiskLimits(daily_notional_limit=1_000_000))
    acct = AccountState(
        deposit=10_000_000,
        total_equity=10_000_000,
        day_start_equity=10_000_000,
        today_notional_used=1_000_000,
    )
    d = rm.evaluate(_buy(), price=70_000, account=acct)
    assert d.decision is Decision.REJECT
    assert "daily notional" in d.reason


def test_should_force_liquidate_active_when_kill_switch_on_and_loss_hit() -> None:
    """daily_loss_kill=True + 손실 한도 도달 → True."""
    rm = RiskManager(limits=RiskLimits(daily_loss_limit=0.03, daily_loss_kill=True))
    acct = AccountState(
        deposit=9_500_000,
        total_equity=9_500_000,  # -5% 손실
        day_start_equity=10_000_000,
    )
    assert rm.should_force_liquidate(acct) is True


def test_should_force_liquidate_inactive_when_kill_switch_off() -> None:
    """daily_loss_kill=False (기본) — 손실 한도 도달해도 False."""
    rm = RiskManager(limits=RiskLimits(daily_loss_limit=0.03, daily_loss_kill=False))
    acct = AccountState(
        deposit=9_500_000,
        total_equity=9_500_000,
        day_start_equity=10_000_000,
    )
    assert rm.should_force_liquidate(acct) is False


def test_should_force_liquidate_false_within_threshold() -> None:
    """kill switch 활성이라도 손실이 한도 안이면 False."""
    rm = RiskManager(limits=RiskLimits(daily_loss_limit=0.03, daily_loss_kill=True))
    acct = AccountState(
        deposit=9_800_000,
        total_equity=9_800_000,  # -2% (한도 -3% 안)
        day_start_equity=10_000_000,
    )
    assert rm.should_force_liquidate(acct) is False


# ---- 29단계 PR27 (#15 Phase 1) — 상관계수 가드 ------------------------------


def test_buy_rejected_when_correlation_exceeds_limit() -> None:
    """기존 보유와 상관 0.85 (한도 0.7 초과) → REJECT."""
    rm = RiskManager(limits=RiskLimits(max_position_correlation=0.7, max_portfolio_exposure=1.0))
    acct = AccountState(
        deposit=10_000_000,
        total_equity=10_000_000,
        day_start_equity=10_000_000,
        positions={"AAA": 10},  # 보유 1종목
        avg_prices={"AAA": 50_000.0},
    )
    corr = {("AAA", "TGT"): 0.85}
    d = rm.evaluate(_buy("TGT"), price=70_000, account=acct, holding_correlations=corr)
    assert d.decision is Decision.REJECT
    assert "correlation guard" in d.reason
    assert "AAA" in d.reason


def test_buy_approved_when_correlation_below_limit() -> None:
    """상관 0.5 (한도 0.7 미만) → 통과."""
    rm = RiskManager(limits=RiskLimits(max_position_correlation=0.7, max_portfolio_exposure=1.0))
    acct = AccountState(
        deposit=10_000_000,
        total_equity=10_000_000,
        day_start_equity=10_000_000,
        positions={"AAA": 10},
        avg_prices={"AAA": 50_000.0},
    )
    corr = {("AAA", "TGT"): 0.5}
    d = rm.evaluate(_buy("TGT"), price=70_000, account=acct, holding_correlations=corr)
    assert d.decision is Decision.APPROVE


def test_buy_approved_when_correlation_guard_disabled() -> None:
    """max_position_correlation=0 (기본) 이면 페어 데이터 무시 — 기존 동작."""
    rm = RiskManager(limits=RiskLimits(max_portfolio_exposure=1.0))
    acct = AccountState(
        deposit=10_000_000,
        total_equity=10_000_000,
        day_start_equity=10_000_000,
        positions={"AAA": 10},
        avg_prices={"AAA": 50_000.0},
    )
    corr = {("AAA", "TGT"): 0.99}  # 활성이었으면 거절될 값
    d = rm.evaluate(_buy("TGT"), price=70_000, account=acct, holding_correlations=corr)
    assert d.decision is Decision.APPROVE


def test_buy_approved_when_correlation_data_missing() -> None:
    """한도는 켜져있지만 페어 데이터 None → 가드 스킵 (러너가 데이터 못 모은 부팅 직후 등)."""
    rm = RiskManager(limits=RiskLimits(max_position_correlation=0.7, max_portfolio_exposure=1.0))
    acct = AccountState(
        deposit=10_000_000,
        total_equity=10_000_000,
        day_start_equity=10_000_000,
        positions={"AAA": 10},
        avg_prices={"AAA": 50_000.0},
    )
    d = rm.evaluate(_buy("TGT"), price=70_000, account=acct, holding_correlations=None)
    assert d.decision is Decision.APPROVE


def test_buy_correlation_ignores_zero_qty_holdings() -> None:
    """positions 에 수량 0 잔존(매도 후 dict 미정리) 인 종목은 가드 비교 대상 X."""
    rm = RiskManager(limits=RiskLimits(max_position_correlation=0.7, max_portfolio_exposure=1.0))
    acct = AccountState(
        deposit=10_000_000,
        total_equity=10_000_000,
        day_start_equity=10_000_000,
        positions={"AAA": 0, "BBB": 5},  # AAA 수량 0
        avg_prices={"AAA": 50_000.0, "BBB": 30_000.0},
    )
    # AAA 와 강한 상관이 있어도 수량 0 이라 스킵, BBB 와는 약함 → 통과.
    corr = {("AAA", "TGT"): 0.95, ("BBB", "TGT"): 0.3}
    d = rm.evaluate(_buy("TGT"), price=70_000, account=acct, holding_correlations=corr)
    assert d.decision is Decision.APPROVE


def test_buy_correlation_guard_does_not_block_sell() -> None:
    """가드는 BUY 만. SELL 은 상관계수와 무관."""
    rm = RiskManager(limits=RiskLimits(max_position_correlation=0.7))
    acct = AccountState(
        deposit=10_000_000,
        total_equity=10_000_000,
        day_start_equity=10_000_000,
        positions={"TGT": 10, "AAA": 5},
        avg_prices={"TGT": 70_000.0, "AAA": 50_000.0},
    )
    corr = {("AAA", "TGT"): 0.99}
    d = rm.evaluate(_sell("TGT"), price=70_000, account=acct, holding_correlations=corr)
    assert d.decision is Decision.APPROVE
