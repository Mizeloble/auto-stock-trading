"""PositionGuard — stop-loss / 트레일링 / 부분 익절 결정 단위 테스트."""

from __future__ import annotations

from auto_trading.risk.manager import RiskLimits
from auto_trading.risk.position_guard import GuardReason, PositionGuard


def _guard(**limits_kwargs: float | bool) -> PositionGuard:
    return PositionGuard(limits=RiskLimits(**limits_kwargs))


def test_inactive_when_all_params_zero() -> None:
    """기본값(전부 0) — guard 가 비활성. check 항상 None."""
    g = _guard()
    assert g.is_active is False
    action = g.check(symbol="A", price=120.0, quantity=10, avg_price=100.0)
    assert action is None


def test_stop_loss_fires_on_drop_below_threshold() -> None:
    """매수가 100 → 99 (-1.0%) 도달 시 stop_loss=0.01 발화."""
    g = _guard(stop_loss_pct=0.01)
    # 첫 호출은 등록만(가격이 매수가와 같으면 발화 X)
    assert g.check(symbol="A", price=100.0, quantity=10, avg_price=100.0) is None
    action = g.check(symbol="A", price=99.0, quantity=10, avg_price=100.0)
    assert action is not None
    assert action.reason is GuardReason.STOP_LOSS
    assert action.quantity == 10  # 전량


def test_stop_loss_does_not_fire_just_above_threshold() -> None:
    g = _guard(stop_loss_pct=0.01)
    g.check(symbol="A", price=100.0, quantity=10, avg_price=100.0)
    # -0.5% — 한도 -1% 안.
    assert g.check(symbol="A", price=99.5, quantity=10, avg_price=100.0) is None


def test_trailing_stop_fires_on_drop_from_high() -> None:
    """가격 100 → 110 → 108.0 (고점 -1.8%) 시 트레일링 발화 (한도 -1%)."""
    g = _guard(trailing_stop_pct=0.01)
    g.check(symbol="A", price=100.0, quantity=10, avg_price=100.0)
    g.check(symbol="A", price=110.0, quantity=10, avg_price=100.0)  # 신고점 갱신
    action = g.check(symbol="A", price=108.0, quantity=10, avg_price=100.0)
    assert action is not None
    assert action.reason is GuardReason.TRAILING
    assert action.quantity == 10


def test_trailing_does_not_fire_when_holding_above_high() -> None:
    g = _guard(trailing_stop_pct=0.02)
    g.check(symbol="A", price=100.0, quantity=10, avg_price=100.0)
    g.check(symbol="A", price=110.0, quantity=10, avg_price=100.0)
    # 109.0 — 110 대비 -0.91%, 한도 -2% 안.
    assert g.check(symbol="A", price=109.0, quantity=10, avg_price=100.0) is None


def test_partial_take_fires_at_target_with_half_qty() -> None:
    """매수가 100 → 102 (+2%) 도달 시 partial_take=0.02 발화, ratio=0.5 → 5주 매도."""
    g = _guard(partial_take_pct=0.02, partial_take_qty_ratio=0.5)
    g.check(symbol="A", price=100.0, quantity=10, avg_price=100.0)
    action = g.check(symbol="A", price=102.0, quantity=10, avg_price=100.0)
    assert action is not None
    assert action.reason is GuardReason.PARTIAL_TAKE
    assert action.quantity == 5


def test_partial_take_fires_only_once_then_trailing_takes_over() -> None:
    """partial 후엔 partial 재발화 X. 트레일링은 같이 활성이면 별도로 발화."""
    g = _guard(partial_take_pct=0.02, partial_take_qty_ratio=0.5, trailing_stop_pct=0.01)
    # 등록 + 부분 익절
    g.check(symbol="A", price=100.0, quantity=10, avg_price=100.0)
    a1 = g.check(symbol="A", price=102.0, quantity=10, avg_price=100.0)
    assert a1 is not None and a1.reason is GuardReason.PARTIAL_TAKE
    # 잔량 5주 — 더 올라가도 partial 재발화 X.
    a2 = g.check(symbol="A", price=104.0, quantity=5, avg_price=100.0)
    assert a2 is None
    # 고점 104 → 102.96 (트레일링 -1%) 도달 시 잔량 트레일링 발화.
    a3 = g.check(symbol="A", price=102.95, quantity=5, avg_price=100.0)
    assert a3 is not None
    assert a3.reason is GuardReason.TRAILING
    assert a3.quantity == 5


def test_stop_loss_priority_over_trailing_when_both_active() -> None:
    """매수가 100, 고점 100, 가격 99 — stop_loss 와 trailing 모두 한도지만 stop_loss 우선."""
    g = _guard(stop_loss_pct=0.01, trailing_stop_pct=0.005)
    g.check(symbol="A", price=100.0, quantity=10, avg_price=100.0)
    action = g.check(symbol="A", price=99.0, quantity=10, avg_price=100.0)
    assert action is not None
    assert action.reason is GuardReason.STOP_LOSS


def test_check_returns_none_when_quantity_zero() -> None:
    """보유 0 이면 추적 자동 해제 + None."""
    g = _guard(stop_loss_pct=0.01)
    g.check(symbol="A", price=100.0, quantity=10, avg_price=100.0)
    assert g.check(symbol="A", price=99.0, quantity=0, avg_price=100.0) is None
    # 다음 진입 시 새 추적이 시작되는지 확인 — 99.0 에 다시 들어와도 stop_loss 발화 X.
    assert g.check(symbol="A", price=99.0, quantity=10, avg_price=99.0) is None


def test_reset_clears_tracking_per_symbol() -> None:
    """reset(symbol) 호출 후 새 진입 시 highest_seen 리셋."""
    g = _guard(trailing_stop_pct=0.01)
    g.check(symbol="A", price=100.0, quantity=10, avg_price=100.0)
    g.check(symbol="A", price=110.0, quantity=10, avg_price=100.0)
    g.reset(symbol="A")
    # 재진입 — 99.5 가 들어와도 highest 가 리셋됐으므로 발화 X.
    g.check(symbol="A", price=99.0, quantity=10, avg_price=99.0)
    assert g.check(symbol="A", price=98.5, quantity=10, avg_price=99.0) is None


def test_partial_take_full_ratio_sells_all_and_marks_taken() -> None:
    """ratio=1.0 이면 부분이 아니라 전량 — quantity 전부 매도."""
    g = _guard(partial_take_pct=0.02, partial_take_qty_ratio=1.0)
    g.check(symbol="A", price=100.0, quantity=10, avg_price=100.0)
    action = g.check(symbol="A", price=102.0, quantity=10, avg_price=100.0)
    assert action is not None
    assert action.quantity == 10
