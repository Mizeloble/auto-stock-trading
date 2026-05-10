"""runner ↔ ConsecutiveLossTracker 통합 회귀.

29단계 PR10 (#14 2차).

검증:
- RiskLimits.max_consecutive_losses > 0 면 __post_init__ 가 자동 트래커 생성.
- 0 이면 트래커 None 그대로 (비활성).
- 명시 주입된 트래커는 자동 생성에 덮어쓰이지 않음.
- SELL 손절 N번 연속 → 트래커가 paused 진입 + risk.consecutive_loss_pause 알림.
- paused 상태에서 BUY 시그널 → _evaluate_signal 차단 (None 반환).
- paused 상태에서 SELL 시그널 → 차단되지 않음 (청산은 허용).
- unpause_loss_tracker() 가 paused 해제 + True 반환.
- 비활성 트래커에서 unpause_loss_tracker() → False.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import auto_trading.runner as runner_module
from auto_trading._common import KST, Bar, Side
from auto_trading.account.orders import Order, OrderResult, OrderType
from auto_trading.risk.consecutive_loss import ConsecutiveLossTracker
from auto_trading.risk.manager import RiskLimits, RiskManager
from auto_trading.runner import PaperRunner
from auto_trading.strategy.base import Signal
from auto_trading.strategy.sma_crossover import SmaCrossover


def _runner_with_consec_loss(threshold: int = 2) -> PaperRunner:
    return PaperRunner(
        strategy=SmaCrossover(),
        symbols=["005930"],
        risk=RiskManager(
            limits=RiskLimits(max_consecutive_losses=threshold, consecutive_loss_pause_sec=3600)
        ),
    )


def _bar(close: float = 70_000.0) -> Bar:
    return Bar(
        symbol="005930",
        ts=datetime.now(KST),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1_000,
    )


def _account(buy_price: float = 70_000.0, qty: int = 5) -> runner_module.AccountState:
    return runner_module.AccountState(
        deposit=10_000_000,
        total_equity=10_000_000,
        positions={"005930": qty},
        avg_prices={"005930": buy_price},
    )


# ---- __post_init__ auto-init -------------------------------------------------


def test_loss_tracker_auto_created_when_limits_active() -> None:
    r = _runner_with_consec_loss(threshold=3)
    assert r.loss_tracker is not None
    assert r.loss_tracker.threshold == 3
    assert r.loss_tracker.pause_seconds == 3600


def test_loss_tracker_not_created_when_limits_zero() -> None:
    r = PaperRunner(
        strategy=SmaCrossover(),
        symbols=["005930"],
        # max_consecutive_losses default 0
    )
    assert r.loss_tracker is None


def test_explicit_loss_tracker_not_overwritten() -> None:
    """명시 주입된 트래커는 RiskLimits 기본값과 무관하게 보존."""
    custom = ConsecutiveLossTracker(threshold=5, pause_seconds=120)
    r = PaperRunner(
        strategy=SmaCrossover(),
        symbols=["005930"],
        risk=RiskManager(limits=RiskLimits(max_consecutive_losses=3)),
        loss_tracker=custom,
    )
    assert r.loss_tracker is custom
    assert r.loss_tracker.threshold == 5  # 자동 생성으로 덮이지 않음


# ---- _place_and_track SELL → tracker 갱신 + 알림 -----------------------------


async def test_sell_loss_increments_streak_and_notifies_on_pause() -> None:
    """2연속 손절 후 paused 진입 → risk.consecutive_loss_pause 알림."""
    r = _runner_with_consec_loss(threshold=2)
    fake_notifier = AsyncMock()
    r.notifier = fake_notifier

    # 1번째 손절: 매수가 70000, 매도가 65000 → -5000 * 5 = -25000.
    sell_order = Order(symbol="005930", side=Side.SELL, quantity=5, order_type=OrderType.MARKET)
    bar = _bar(close=65_000.0)
    fake_result = OrderResult(order_no="X1", raw={}, client_id="c1")
    with patch.object(runner_module, "place_order", AsyncMock(return_value=fake_result)):
        await r._place_and_track(rest=object(), order=sell_order, bar=bar, account=_account())  # type: ignore[arg-type]

    assert r.loss_tracker is not None
    assert r.loss_tracker.streak == 1
    assert r.loss_tracker.is_paused() is False
    # 첫 손절은 paused 진입 X — alert 호출 안 됨.
    pause_events = [
        c for c in fake_notifier.notify.call_args_list if c.args[0] == "risk.consecutive_loss_pause"
    ]
    assert not pause_events

    # 2번째 손절 → paused 진입 + 알림.
    with patch.object(runner_module, "place_order", AsyncMock(return_value=fake_result)):
        await r._place_and_track(rest=object(), order=sell_order, bar=bar, account=_account())  # type: ignore[arg-type]

    assert r.loss_tracker.is_paused() is True
    pause_events = [
        c for c in fake_notifier.notify.call_args_list if c.args[0] == "risk.consecutive_loss_pause"
    ]
    assert len(pause_events) == 1
    _, kwargs = pause_events[0]
    assert kwargs["streak"] == 2


async def test_sell_profit_resets_streak() -> None:
    r = _runner_with_consec_loss(threshold=3)
    r.notifier = AsyncMock()
    sell_order = Order(symbol="005930", side=Side.SELL, quantity=5, order_type=OrderType.MARKET)
    fake_result = OrderResult(order_no="X1", raw={}, client_id="c1")

    # 손절 1번.
    with patch.object(runner_module, "place_order", AsyncMock(return_value=fake_result)):
        await r._place_and_track(
            rest=object(),  # type: ignore[arg-type]
            order=sell_order,
            bar=_bar(close=65_000.0),
            account=_account(),
        )
    assert r.loss_tracker is not None
    assert r.loss_tracker.streak == 1

    # 익절 → streak reset.
    with patch.object(runner_module, "place_order", AsyncMock(return_value=fake_result)):
        await r._place_and_track(
            rest=object(),  # type: ignore[arg-type]
            order=sell_order,
            bar=_bar(close=75_000.0),
            account=_account(),
        )
    assert r.loss_tracker.streak == 0


async def test_sell_with_disabled_tracker_does_nothing() -> None:
    """loss_tracker 가 None 이면 SELL 후에도 아무 일 없음 (회귀 0)."""
    r = PaperRunner(strategy=SmaCrossover(), symbols=["005930"])
    r.notifier = AsyncMock()
    assert r.loss_tracker is None
    sell_order = Order(symbol="005930", side=Side.SELL, quantity=5, order_type=OrderType.MARKET)
    fake_result = OrderResult(order_no="X1", raw={}, client_id="c1")
    with patch.object(runner_module, "place_order", AsyncMock(return_value=fake_result)):
        await r._place_and_track(
            rest=object(),  # type: ignore[arg-type]
            order=sell_order,
            bar=_bar(close=65_000.0),
            account=_account(),
        )
    assert r.loss_tracker is None  # 여전히 None


# ---- _evaluate_signal BUY 차단 -----------------------------------------------


async def test_evaluate_signal_blocks_buy_when_paused() -> None:
    r = _runner_with_consec_loss(threshold=2)
    # paused 강제 진입.
    assert r.loss_tracker is not None
    r.loss_tracker.record_round_trip(-100)
    r.loss_tracker.record_round_trip(-100)
    assert r.loss_tracker.is_paused()

    signal = Signal(symbol="005930", side=Side.BUY, weight=0.5, price_hint=Decimal("70000"))
    order = await r._evaluate_signal(signal, _bar(), _account())
    assert order is None


async def test_evaluate_signal_allows_sell_when_paused() -> None:
    """paused 라도 SELL(청산) 은 막지 않음 — 손절 차단은 의도와 반대."""
    r = _runner_with_consec_loss(threshold=2)
    assert r.loss_tracker is not None
    r.loss_tracker.record_round_trip(-100)
    r.loss_tracker.record_round_trip(-100)
    assert r.loss_tracker.is_paused()

    sell_signal = Signal(symbol="005930", side=Side.SELL, weight=1.0, price_hint=Decimal("70000"))
    # SELL 은 risk.evaluate 까지 도달 — None 또는 Order 가능. 핵심은 *consec_loss 가 차단 안 함*.
    # consec_loss 가 차단했다면 risk.evaluate 가 안 불림. 이걸 검증하기 위해 risk.evaluate 를 mock 해서
    # "호출됐는지" 만 확인.
    with patch.object(r.risk, "evaluate", wraps=r.risk.evaluate) as spy:
        await r._evaluate_signal(sell_signal, _bar(), _account())
    spy.assert_called_once()  # consec_loss 가드를 통과해 risk.evaluate 까지 도달


async def test_evaluate_signal_allows_buy_when_not_paused() -> None:
    r = _runner_with_consec_loss(threshold=3)
    assert r.loss_tracker is not None
    r.loss_tracker.record_round_trip(-100)  # streak=1, 아직 paused 아님

    signal = Signal(symbol="005930", side=Side.BUY, weight=0.05, price_hint=Decimal("70000"))
    with patch.object(r.risk, "evaluate", wraps=r.risk.evaluate) as spy:
        await r._evaluate_signal(signal, _bar(), _account(buy_price=70_000.0, qty=0))
    spy.assert_called_once()


# ---- unpause_loss_tracker ---------------------------------------------------


def test_unpause_returns_true_when_paused() -> None:
    r = _runner_with_consec_loss(threshold=2)
    assert r.loss_tracker is not None
    r.loss_tracker.record_round_trip(-100)
    r.loss_tracker.record_round_trip(-100)
    assert r.loss_tracker.is_paused()

    assert r.unpause_loss_tracker() is True
    assert r.loss_tracker.is_paused() is False
    assert r.loss_tracker.streak == 0


def test_unpause_returns_false_when_not_paused() -> None:
    r = _runner_with_consec_loss(threshold=3)
    # 트래커는 활성이지만 paused 아님.
    assert r.unpause_loss_tracker() is False


def test_unpause_returns_false_when_tracker_disabled() -> None:
    r = PaperRunner(strategy=SmaCrossover(), symbols=["005930"])
    assert r.loss_tracker is None
    assert r.unpause_loss_tracker() is False
