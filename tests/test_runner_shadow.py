"""섀도우 세션(게이트 차단일 주문 없는 가상 매매) — PaperRunner 통합 회귀.

핵심 검증:
- enable_shadow() 가 dry_run 을 강제하고, ws 시작 후 호출은 거부.
- _snapshot_account 가 키움 REST(get_balance) 를 전혀 부르지 않고 가상 원장으로
  AccountState 를 만든다 (day-state 디스크도 무접촉).
- 시뮬 BUY 가 tracker 와 account 포지션에 즉시 반영 → 같은 세션에서 guard/청산이
  실제처럼 돌 수 있다 (일반 dry_run 은 포지션 미반영이라 출구가 안 돌던 것과 대비).
- guard SELL 이 tracker FIFO 실현손익 + churn 브레이크(_daily_realized_net)에 적립.
- liquidate_all(dry_run) 이 마지막 봉 종가로 가상 청산 체결을 남긴다.
"""

from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from auto_trading._common import KST, Bar, Side
from auto_trading.risk.manager import AccountState, RiskLimits, RiskManager
from auto_trading.risk.position_guard import GuardAction, GuardReason
from auto_trading.runner import PaperRunner
from auto_trading.runner import paper as runner_module
from auto_trading.shadow import ShadowTracker
from auto_trading.strategy.base import Signal
from auto_trading.strategy.sma_crossover import SmaCrossover


def _shadow_runner(**limits_kw) -> tuple[PaperRunner, ShadowTracker]:
    r = PaperRunner(
        strategy=SmaCrossover(),
        symbols=["005930"],
        risk=RiskManager(limits=RiskLimits(**limits_kw)),
        liquidate_at=time(15, 15, tzinfo=KST),
    )
    tracker = ShadowTracker(initial_cash=10_000_000.0, slippage_bps=0.0)
    r.enable_shadow(tracker)
    return r, tracker


def _bar(close: float = 70_000.0, symbol: str = "005930") -> Bar:
    return Bar(
        symbol=symbol,
        ts=datetime.now(KST),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1_000,
    )


def _account() -> AccountState:
    return AccountState(
        deposit=10_000_000,
        total_equity=10_000_000,
        positions={},
        avg_prices={},
        day_start_equity=10_000_000,
    )


def test_enable_shadow_forces_dry_run() -> None:
    r = PaperRunner(strategy=SmaCrossover(), symbols=["005930"])
    assert r.dry_run is False
    r.enable_shadow(ShadowTracker())
    assert r.dry_run is True


def test_enable_shadow_rejected_after_ws_start() -> None:
    r = PaperRunner(strategy=SmaCrossover(), symbols=["005930"])
    r._ws = object()  # type: ignore[assignment]
    with pytest.raises(RuntimeError):
        r.enable_shadow(ShadowTracker())


async def test_snapshot_account_uses_tracker_without_rest() -> None:
    r, tracker = _shadow_runner()
    with patch.object(runner_module, "get_balance", AsyncMock()) as mock_balance:
        state = await r._snapshot_account(object(), previous=None)  # type: ignore[arg-type]
    mock_balance.assert_not_called()
    assert state.deposit == tracker.cash == 10_000_000.0
    assert state.positions == {}
    assert state.day_start_equity == tracker.initial_cash


async def test_simulated_buy_updates_tracker_and_account_positions() -> None:
    r, tracker = _shadow_runner()
    account = _account()
    signal = Signal(symbol="005930", side=Side.BUY, weight=0.1, price_hint=Decimal("70000"))

    order = await r._evaluate_signal(signal, _bar(70_000.0), account)

    assert order is None  # dry_run — REST 로 나갈 주문 없음
    assert tracker.holdings.get("005930", 0) > 0
    # account 에 즉시 미러 — 같은 봉부터 guard 가 잔량을 본다.
    assert account.positions["005930"] == tracker.holdings["005930"]
    assert account.avg_prices["005930"] == pytest.approx(tracker.avg_prices["005930"])
    # 다음 잔고 갱신도 tracker 를 그대로 반영 (실잔고 덮어쓰기 없음).
    state = await r._snapshot_account(object(), previous=account)  # type: ignore[arg-type]
    assert state.positions["005930"] == tracker.holdings["005930"]


async def test_guard_sell_applies_tracker_and_churn_brake() -> None:
    r, tracker = _shadow_runner()
    account = _account()
    # 선행 시뮬 매수 — tracker/account 에 포지션 형성.
    buy = Signal(symbol="005930", side=Side.BUY, weight=0.1, price_hint=Decimal("70000"))
    await r._evaluate_signal(buy, _bar(70_000.0), account)
    qty = account.positions["005930"]
    assert qty > 0

    # 손절 가격으로 guard SELL 발화 (실주문 없이 시뮬 경로).
    action = GuardAction(
        symbol="005930",
        quantity=qty,
        reason=GuardReason.STOP_LOSS,
        price=69_000.0,
    )
    with patch.object(runner_module, "place_order") as mock_place:
        await r._place_guard_action(object(), action, _bar(69_000.0), account)  # type: ignore[arg-type]

    mock_place.assert_not_called()
    assert account.positions["005930"] == 0
    assert tracker.holdings.get("005930", 0) == 0
    # FIFO 실현손실 + churn 브레이크 적립 (음수).
    assert tracker.realized_pnl < 0
    assert r._daily_realized_net < 0


async def test_liquidate_all_records_virtual_sell_at_last_mark() -> None:
    r, tracker = _shadow_runner()
    account = _account()
    buy = Signal(symbol="005930", side=Side.BUY, weight=0.1, price_hint=Decimal("70000"))
    await r._evaluate_signal(buy, _bar(70_000.0), account)
    qty = account.positions["005930"]
    r._shadow_marks["005930"] = 71_000.0  # 마지막 봉 종가

    with patch.object(runner_module, "place_order") as mock_place:
        await r.liquidate_all(object(), account, reason="test")  # type: ignore[arg-type]

    mock_place.assert_not_called()
    assert account.positions["005930"] == 0
    assert tracker.holdings.get("005930", 0) == 0
    # 71,000 원 가상 체결 — 70,000 매수 대비 실현이익 (수수료 차감해도 양수).
    assert tracker.realized_pnl > 0
    assert tracker.num_signals == 2  # BUY 1 + 청산 SELL 1
    del qty


def test_state_snapshot_carries_shadow_flag() -> None:
    r, _ = _shadow_runner()
    assert r.state_snapshot().shadow is True
    plain = PaperRunner(strategy=SmaCrossover(), symbols=["005930"])
    assert plain.state_snapshot().shadow is False
