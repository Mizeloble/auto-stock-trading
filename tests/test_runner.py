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


def _snapshot(
    deposit: int = 1_000_000,
    equity: int = 1_500_000,
    *,
    holdings: list[Holding] | None = None,
) -> AccountSnapshot:
    return AccountSnapshot(
        deposit=Decimal(deposit),
        total_evaluation=Decimal(equity),
        total_pnl=Decimal(0),
        holdings=holdings
        if holdings is not None
        else [
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
    # total_equity 는 예수금 + 보유 평가금 합계 (deposit 1_000_000 + market_value 650_000).
    assert state.total_equity == 1_650_000
    assert state.day_start_equity == 1_650_000  # 최초 스냅샷이므로 오늘 시작치로 고정


async def test_snapshot_preserves_day_start_equity_across_refreshes() -> None:
    """하루 시작 에쿼티는 첫 스냅샷 값을 계속 유지해야 일일 손실 한도가 정확하게 계산된다."""
    r = _runner()

    holdings_650k = [
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
    ]
    holdings_550k = [
        Holding(
            symbol="005930",
            name="삼성전자",
            quantity=10,
            avg_price=Decimal("60000"),
            current_price=Decimal("55000"),
            market_value=Decimal("550000"),
            pnl=Decimal("-50000"),
            pnl_pct=Decimal("-8.3"),
        ),
    ]

    with patch.object(
        runner_module,
        "get_balance",
        return_value=_snapshot(deposit=1_000_000, holdings=holdings_650k),
    ):
        first = await r._snapshot_account(rest=object(), previous=None)  # type: ignore[arg-type]

    with patch.object(
        runner_module,
        "get_balance",
        return_value=_snapshot(deposit=1_000_000, holdings=holdings_550k),
    ):
        second = await r._snapshot_account(rest=object(), previous=first)  # type: ignore[arg-type]

    assert first.day_start_equity == 1_650_000  # 1_000_000 cash + 650_000 holdings
    assert second.day_start_equity == 1_650_000  # 유지
    assert second.total_equity == 1_550_000  # 1_000_000 cash + 550_000 holdings


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


async def test_snapshot_total_equity_stable_across_partial_sell() -> None:
    """2026-05-11 회귀: 부분 익절 직후 today_change 가 -50% 로 잘못 계산돼 kill switch 가
    오발동한 사건. total_equity 가 'deposit + holdings_market_value' 로 계산되면 매도가
    cash 증가·holdings 감소로 상쇄돼 안정해야 한다.

    시나리오:
    1) 12주 보유, 현재가 284,500 → holdings 3,414,000 + cash 46,694,000 → equity 50,108,000
    2) 부분 익절 6주 매도 → holdings 1,707,000 + cash 48,401,000 → equity 50,108,000
       (즉 day_start_equity 와의 비율이 거의 0% — kill switch 발화 X)
    """
    r = _runner()

    before_partial = [
        Holding(
            symbol="005930",
            name="삼성전자",
            quantity=12,
            avg_price=Decimal("275500"),
            current_price=Decimal("284500"),
            market_value=Decimal("3414000"),
            pnl=Decimal("108000"),
            pnl_pct=Decimal("3.27"),
        ),
    ]
    after_partial = [
        Holding(
            symbol="005930",
            name="삼성전자",
            quantity=6,
            avg_price=Decimal("275500"),
            current_price=Decimal("284500"),
            market_value=Decimal("1707000"),
            pnl=Decimal("54000"),
            pnl_pct=Decimal("3.27"),
        ),
    ]
    snap_before = AccountSnapshot(
        deposit=Decimal("46694000"),
        total_evaluation=Decimal("3414000"),
        total_pnl=Decimal("108000"),
        holdings=before_partial,
    )
    snap_after = AccountSnapshot(
        deposit=Decimal("48401000"),
        total_evaluation=Decimal("1707000"),
        total_pnl=Decimal("54000"),
        holdings=after_partial,
    )

    with patch.object(runner_module, "get_balance", return_value=snap_before):
        first = await r._snapshot_account(rest=object(), previous=None)  # type: ignore[arg-type]
    with patch.object(runner_module, "get_balance", return_value=snap_after):
        second = await r._snapshot_account(rest=object(), previous=first)  # type: ignore[arg-type]

    # day_start_equity 는 첫 스냅샷 시점 fixed.
    assert first.day_start_equity == 50_108_000
    assert second.day_start_equity == 50_108_000
    # 부분 익절 직후 total_equity 가 거의 그대로 — kill switch 의 today_change 거의 0.
    assert second.total_equity == pytest.approx(50_108_000)
    today_change = second.total_equity / second.day_start_equity - 1.0
    assert abs(today_change) < 0.001  # 1‰ 미만, -3% 한도와 한참 멀어야


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


async def test_install_signal_handlers_routes_sigterm_and_sigint_to_request_stop() -> None:
    """SIGTERM/SIGINT 둘 다 request_stop 으로 라우팅돼야 launchd paper-stop 후
    finally 블록(runner.stopped 푸시) 이 정상 동작."""
    import signal as _signal

    from auto_trading.runner import RunnerControlState

    r = _runner()
    registered: dict[int, object] = {}

    class _LoopStub:
        def add_signal_handler(self, sig: int, callback: object) -> None:
            registered[sig] = callback

    with patch("auto_trading.runner.asyncio.get_running_loop", return_value=_LoopStub()):
        r._install_signal_handlers()

    # 핸들러가 등록됐고, 호출하면 실제로 STOPPING 진입.
    assert _signal.SIGTERM in registered
    assert _signal.SIGINT in registered
    assert callable(registered[_signal.SIGTERM])

    assert r._control_state is RunnerControlState.RUNNING
    registered[_signal.SIGTERM]()  # type: ignore[operator]
    assert r._control_state is RunnerControlState.STOPPING


async def test_install_signal_handlers_tolerates_unsupported_platform() -> None:
    """Windows/별도 스레드에서 add_signal_handler 가 실패해도 silently swallow."""
    r = _runner()

    class _FailingLoop:
        def add_signal_handler(self, sig: int, callback: object) -> None:
            raise NotImplementedError("not supported")

    with patch("auto_trading.runner.asyncio.get_running_loop", return_value=_FailingLoop()):
        r._install_signal_handlers()  # raise 하면 실패

    # 별도 어설션 없이 통과 = swallow 정상


async def test_request_stop_signals_active_ws_to_unblock_main_loop() -> None:
    """request_stop 은 ws.signal_stop 을 호출해 ticks() 를 즉시 종료시켜야 한다.

    이게 빠지면 4/30 launchd auto-stop 처럼 main loop 가 다음 tick 을 영원히 기다림.
    """

    class _StubWs:
        def __init__(self) -> None:
            self.signal_stop_called = False

        def signal_stop(self) -> None:
            self.signal_stop_called = True

    r = _runner()
    fake_ws = _StubWs()
    r._ws = fake_ws  # type: ignore[assignment]
    r.request_stop()

    assert fake_ws.signal_stop_called, "request_stop 이 ws.signal_stop 을 호출해야 함"


async def test_request_stop_without_active_ws_is_noop() -> None:
    """run() 시작 전 또는 finally 진입 후엔 _ws 가 None — request_stop 이 attribute
    오류 없이 STOPPING 만 진입해야 한다."""
    from auto_trading.runner import RunnerControlState

    r = _runner()
    assert r._ws is None
    r.request_stop()  # raise 안 해야 함
    assert r._control_state is RunnerControlState.STOPPING
