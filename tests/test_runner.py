"""PaperRunner 의 잔고 스냅샷 폴백 동작 + notifier hook 검증."""

from __future__ import annotations

from decimal import Decimal
from itertools import count
from unittest.mock import AsyncMock, patch

import pytest

from auto_trading import runner as runner_module
from auto_trading.account.balance import AccountSnapshot, Holding
from auto_trading.account.orders import OrderResult
from auto_trading.runner import PaperRunner
from auto_trading.strategy.sma_crossover import SmaCrossover


def _snapshot(deposit: int = 1_000_000, equity: int = 1_500_000) -> AccountSnapshot:
    return AccountSnapshot(
        deposit=Decimal(deposit),
        total_evaluation=Decimal(equity),
        total_pnl=Decimal(0),
        holdings=[
            Holding(
                symbol="005930",
                name="삼성전자",
                quantity=10,
                avg_price=Decimal("60000"),
                current_price=Decimal("65000"),
                market_value=Decimal("650000"),
                pnl=Decimal("50000"),
                pnl_pct=Decimal("8.3"),
            ),
        ],
    )


def _runner(max_failures: int = 3) -> PaperRunner:
    return PaperRunner(
        strategy=SmaCrossover(),
        symbols=["005930"],
        max_balance_failures=max_failures,
    )


async def test_snapshot_success_resets_failure_counter() -> None:
    r = _runner()
    r._balance_failures = 2

    with patch.object(runner_module, "get_balance", return_value=_snapshot()):
        state = await r._snapshot_account(rest=object(), previous=None)  # type: ignore[arg-type]

    assert r._balance_failures == 0
    assert state.deposit == 1_000_000
    assert state.day_start_equity == 1_500_000  # 최초 스냅샷이므로 오늘 시작치로 고정


async def test_snapshot_preserves_day_start_equity_across_refreshes() -> None:
    """하루 시작 에쿼티는 첫 스냅샷 값을 계속 유지해야 일일 손실 한도가 정확하게 계산된다."""
    r = _runner()

    with patch.object(runner_module, "get_balance", return_value=_snapshot(equity=1_500_000)):
        first = await r._snapshot_account(rest=object(), previous=None)  # type: ignore[arg-type]

    with patch.object(runner_module, "get_balance", return_value=_snapshot(equity=1_400_000)):
        second = await r._snapshot_account(rest=object(), previous=first)  # type: ignore[arg-type]

    assert first.day_start_equity == 1_500_000
    assert second.day_start_equity == 1_500_000  # 유지
    assert second.total_equity == 1_400_000


async def test_snapshot_failure_returns_previous_state() -> None:
    r = _runner(max_failures=3)

    with patch.object(runner_module, "get_balance", return_value=_snapshot()):
        good = await r._snapshot_account(rest=object(), previous=None)  # type: ignore[arg-type]

    with patch.object(runner_module, "get_balance", side_effect=RuntimeError("net down")):
        state = await r._snapshot_account(rest=object(), previous=good)  # type: ignore[arg-type]

    assert state is good
    assert r._balance_failures == 1


async def test_snapshot_raises_after_max_consecutive_failures() -> None:
    r = _runner(max_failures=3)

    with patch.object(runner_module, "get_balance", return_value=_snapshot()):
        good = await r._snapshot_account(rest=object(), previous=None)  # type: ignore[arg-type]

    errors = (RuntimeError(f"err{i}") for i in count())

    def _boom(*args, **kwargs):
        raise next(errors)

    with patch.object(runner_module, "get_balance", side_effect=_boom):
        # 1, 2회차는 previous 반환 후 3회차에 RuntimeError
        await r._snapshot_account(rest=object(), previous=good)  # type: ignore[arg-type]
        await r._snapshot_account(rest=object(), previous=good)  # type: ignore[arg-type]
        with pytest.raises(RuntimeError, match="balance fetch failed"):
            await r._snapshot_account(rest=object(), previous=good)  # type: ignore[arg-type]


async def test_snapshot_first_call_failure_propagates() -> None:
    r = _runner(max_failures=3)

    with (
        patch.object(runner_module, "get_balance", side_effect=RuntimeError("net down")),
        pytest.raises(RuntimeError, match="net down"),
    ):
        await r._snapshot_account(rest=object(), previous=None)  # type: ignore[arg-type]


async def test_notifier_called_on_order_accepted() -> None:
    """주문 접수 성공 시 notifier.notify('runner.order_accepted', ...) 가 호출돼야 한다."""
    from auto_trading._common import Bar
    from auto_trading.risk.manager import AccountState
    from auto_trading.strategy.base import Side, Signal

    fake_notifier = AsyncMock()
    r = PaperRunner(strategy=SmaCrossover(), symbols=["005930"], notifier=fake_notifier)
    account = AccountState(
        deposit=10_000_000,
        total_equity=10_000_000,
        day_start_equity=10_000_000,
    )
    bar = Bar(
        symbol="005930",
        ts=__import__("datetime").datetime.now(),
        open=70_000,
        high=70_000,
        low=70_000,
        close=70_000,
        volume=1,
    )
    forced_signal = [Signal(symbol="005930", side=Side.BUY, weight=1.0)]
    r.strategy.on_bar = lambda _b: forced_signal  # type: ignore[method-assign]

    fake_result = OrderResult(order_no="ORD-1", raw={})
    with patch.object(runner_module, "place_order", AsyncMock(return_value=fake_result)):
        await r._on_bar(rest=object(), bar=bar, account=account)  # type: ignore[arg-type]

    events = [call.args[0] for call in fake_notifier.notify.call_args_list]
    assert "runner.order_accepted" in events
    accepted_call = next(
        c for c in fake_notifier.notify.call_args_list if c.args[0] == "runner.order_accepted"
    )
    assert accepted_call.kwargs["order_no"] == "ORD-1"
    assert accepted_call.kwargs["symbol"] == "005930"


async def test_notifier_called_on_balance_failed_fatal() -> None:
    """잔고 누적 실패로 러너 중단 시 notifier 가 'runner.balance_failed_fatal' 알림."""
    fake_notifier = AsyncMock()
    r = PaperRunner(
        strategy=SmaCrossover(),
        symbols=["005930"],
        max_balance_failures=2,
        notifier=fake_notifier,
    )

    with patch.object(runner_module, "get_balance", return_value=_snapshot()):
        good = await r._snapshot_account(rest=object(), previous=None)  # type: ignore[arg-type]

    with patch.object(runner_module, "get_balance", side_effect=RuntimeError("net down")):
        # 1회차는 previous 반환
        await r._snapshot_account(rest=object(), previous=good)  # type: ignore[arg-type]
        # 2회차에 max 도달 → fatal raise + notifier 호출
        with pytest.raises(RuntimeError, match="balance fetch failed"):
            await r._snapshot_account(rest=object(), previous=good)  # type: ignore[arg-type]

    events = [call.args[0] for call in fake_notifier.notify.call_args_list]
    assert "runner.balance_failed_fatal" in events


async def test_dry_run_skips_real_order_placement() -> None:
    """--dry-run 모드는 place_order 를 절대 호출하지 않는다."""
    from auto_trading._common import Bar
    from auto_trading.risk.manager import AccountState
    from auto_trading.strategy.base import Side, Signal

    r = PaperRunner(strategy=SmaCrossover(), symbols=["005930"], dry_run=True)
    account = AccountState(
        deposit=10_000_000,
        total_equity=10_000_000,
        day_start_equity=10_000_000,
    )
    bar = Bar(
        symbol="005930",
        ts=__import__("datetime").datetime.now(),
        open=70_000,
        high=70_000,
        low=70_000,
        close=70_000,
        volume=1,
    )

    # 전략이 아무 시그널도 안 내면 테스트가 무의미하므로 강제 시그널 주입
    forced_signal = [Signal(symbol="005930", side=Side.BUY, weight=1.0)]
    r.strategy.on_bar = lambda _b: forced_signal  # type: ignore[method-assign]

    with patch.object(runner_module, "place_order") as mock_place:
        await r._on_bar(rest=object(), bar=bar, account=account)  # type: ignore[arg-type]

    mock_place.assert_not_called()


# ---- 텔레그램 v3 제어 (RunnerState 머신) ---------------------------------


def test_pause_sets_state_to_paused() -> None:
    r = _runner()
    assert r._state is runner_module.RunnerState.RUNNING
    r.pause()
    assert r._state is runner_module.RunnerState.PAUSED


def test_resume_returns_to_running_from_paused() -> None:
    r = _runner()
    r.pause()
    r.resume()
    assert r._state is runner_module.RunnerState.RUNNING


def test_request_stop_sets_event_and_state() -> None:
    r = _runner()
    assert not r._stop_event.is_set()
    r.request_stop()
    assert r._state is runner_module.RunnerState.STOPPING
    assert r._stop_event.is_set()


def test_pause_after_stop_request_is_noop() -> None:
    """STOPPING 상태에선 pause/resume 이 무시되어야 한다 (이미 종료 중)."""
    r = _runner()
    r.request_stop()
    r.pause()
    assert r._state is runner_module.RunnerState.STOPPING
    r.resume()
    assert r._state is runner_module.RunnerState.STOPPING


def test_state_snapshot_reflects_current_state() -> None:
    r = _runner()
    assert r.state_snapshot().state == "running"
    r.pause()
    assert r.state_snapshot().state == "paused"
    r.resume()
    assert r.state_snapshot().state == "running"
    r.request_stop()
    assert r.state_snapshot().state == "stopping"
