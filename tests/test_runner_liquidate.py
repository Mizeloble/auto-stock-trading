"""day-trade 청산 훅 (liquidate_all + _liquidation_loop) 회귀 테스트.

핵심 검증:
- dry_run=True 일 때 place_order 가 절대 호출되지 않고 시뮬 로그만
- dry_run=False 일 때 보유 종목마다 시장가 SELL 송신 + 로컬 포지션 0 갱신
- 한 종목 송신 실패가 나머지 종목 청산을 막지 않음
- liquidate_at 시각 도달 시 _liquidation_loop 가 청산 + request_stop 까지 마무리
- 두 번 호출되어도 청산은 1회만 (장 마감 후 시계가 계속 True 라도)
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from unittest.mock import AsyncMock, patch

from auto_trading import runner as runner_module
from auto_trading._common import KST
from auto_trading.account.orders import OrderResult
from auto_trading.risk.manager import AccountState
from auto_trading.runner import PaperRunner, RunnerControlState
from auto_trading.strategy.intraday import IntradayMomentum


def _day_trade_runner(*, dry_run: bool = False, liquidate_grace_sec: int = 0) -> PaperRunner:
    return PaperRunner(
        liquidate_at=time(15, 15, tzinfo=KST),
        liquidate_grace_sec=liquidate_grace_sec,
        dry_run=dry_run,
    )


def _account(positions: dict[str, int]) -> AccountState:
    return AccountState(
        deposit=10_000_000,
        total_equity=10_000_000,
        positions=dict(positions),
        avg_prices={s: 10_000.0 for s in positions},
        day_start_equity=10_000_000,
    )


async def test_liquidate_all_dry_run_skips_place_order_and_zeros_positions() -> None:
    r = _day_trade_runner(dry_run=True)
    account = _account({"005930": 10, "000660": 5})

    with patch.object(runner_module, "place_order") as mock_place:
        await r.liquidate_all(rest=object(), account=account, reason="test")  # type: ignore[arg-type]

    mock_place.assert_not_called()
    # 로컬 포지션은 0 으로 갱신되어 후속 호출이 idempotent.
    assert account.positions == {"005930": 0, "000660": 0}
    assert r._liquidation_done is True


async def test_liquidate_all_real_sends_market_sell_per_position() -> None:
    r = _day_trade_runner(dry_run=False)
    account = _account({"005930": 10, "000660": 5})

    placed_orders: list[object] = []

    async def _fake_place(rest, order):  # type: ignore[no-untyped-def]
        placed_orders.append(order)
        return OrderResult(order_no=f"ORD-{order.symbol}", raw={})

    with patch.object(runner_module, "place_order", AsyncMock(side_effect=_fake_place)):
        await r.liquidate_all(rest=object(), account=account, reason="test")  # type: ignore[arg-type]

    # 두 종목 모두 시장가 SELL 송신
    assert len(placed_orders) == 2
    syms = sorted(o.symbol for o in placed_orders)  # type: ignore[attr-defined]
    assert syms == ["000660", "005930"]
    for o in placed_orders:
        assert o.side.value == "sell"  # type: ignore[attr-defined]
        assert o.order_type.value == "market"  # type: ignore[attr-defined]
    # 송신 성공이면 로컬 포지션 0
    assert account.positions == {"005930": 0, "000660": 0}


async def test_liquidate_all_one_failure_does_not_block_others() -> None:
    r = _day_trade_runner(dry_run=False)
    account = _account({"005930": 10, "000660": 5})

    async def _flaky(rest, order):  # type: ignore[no-untyped-def]
        if order.symbol == "005930":
            raise RuntimeError("kiwoom rejected")
        return OrderResult(order_no="ORD-2", raw={})

    fake_notifier = AsyncMock()
    r.notifier = fake_notifier

    with patch.object(runner_module, "place_order", AsyncMock(side_effect=_flaky)):
        await r.liquidate_all(rest=object(), account=account, reason="test")  # type: ignore[arg-type]

    # 005930 은 실패라 포지션 그대로, 000660 은 성공이라 0
    assert account.positions["005930"] == 10
    assert account.positions["000660"] == 0
    # liquidate_failed notifier 호출 여부
    events = [c.args[0] for c in fake_notifier.notify.call_args_list]
    assert "runner.liquidate_failed" in events
    assert "runner.liquidate_done" in events
    # 2026-05-08 회귀 — done 이벤트의 count 는 성공만, failed 는 실패만, attempted 는 전체.
    done_call = next(
        c for c in fake_notifier.notify.call_args_list if c.args[0] == "runner.liquidate_done"
    )
    assert done_call.kwargs["count"] == 1  # 000660 성공만
    assert done_call.kwargs["failed"] == 1  # 005930 실패
    assert done_call.kwargs["attempted"] == 2


async def test_liquidate_all_noop_on_empty_positions() -> None:
    r = _day_trade_runner(dry_run=False)
    account = _account({})

    with patch.object(runner_module, "place_order") as mock_place:
        await r.liquidate_all(rest=object(), account=account, reason="test")  # type: ignore[arg-type]

    mock_place.assert_not_called()
    assert r._liquidation_done is True


async def test_liquidate_all_idempotent_second_call_skipped() -> None:
    r = _day_trade_runner(dry_run=True)
    account = _account({"005930": 10})

    await r.liquidate_all(rest=object(), account=account, reason="first")  # type: ignore[arg-type]
    # 두 번째 호출은 즉시 skip — _liquidation_done 가드.
    with patch.object(runner_module, "place_order") as mock_place:
        await r.liquidate_all(rest=object(), account=account, reason="second")  # type: ignore[arg-type]
    mock_place.assert_not_called()


async def test_liquidation_loop_triggers_at_time_and_requests_stop() -> None:
    """가짜 시계로 liquidate_at 도달 → liquidate_all 호출 + STOPPING 진입."""
    r = _day_trade_runner(dry_run=True, liquidate_grace_sec=0)
    r._account = _account({"005930": 10})

    # 현재 시각이 항상 liquidate_at 보다 큰 가짜 시계.
    fake_now = datetime.now(KST).replace(hour=15, minute=20, second=0, microsecond=0)

    class _FakeDatetime:
        @staticmethod
        def now(tz=None):  # type: ignore[no-untyped-def]
            return fake_now

    with patch.object(runner_module, "datetime", _FakeDatetime):
        await r._liquidation_loop(rest=object())  # type: ignore[arg-type]

    assert r._liquidation_done is True
    assert r._control_state is RunnerControlState.STOPPING
    assert r._account.positions["005930"] == 0  # type: ignore[union-attr]


async def test_liquidation_loop_exits_on_external_stop_before_time() -> None:
    """liquidate_at 도달 전에 외부 /stop 들어오면 청산 없이 즉시 종료."""
    r = _day_trade_runner(dry_run=True)
    r._account = _account({"005930": 10})
    r._control_state = RunnerControlState.STOPPING

    with patch.object(runner_module, "place_order") as mock_place:
        await r._liquidation_loop(rest=object())  # type: ignore[arg-type]

    mock_place.assert_not_called()
    assert r._liquidation_done is False
    assert r._account.positions["005930"] == 10  # type: ignore[union-attr]


async def test_liquidation_loop_skipped_when_account_snapshot_missing() -> None:
    """잔고 첫 스냅샷 전이라 _account 가 None 이면 청산 스킵 + 그래도 stop 진입."""
    r = _day_trade_runner(dry_run=True, liquidate_grace_sec=0)
    assert r._account is None

    fake_now = datetime.now(KST).replace(hour=15, minute=20, second=0, microsecond=0)

    class _FakeDatetime:
        @staticmethod
        def now(tz=None):  # type: ignore[no-untyped-def]
            return fake_now

    with (
        patch.object(runner_module, "datetime", _FakeDatetime),
        patch.object(runner_module, "place_order") as mock_place,
    ):
        await r._liquidation_loop(rest=object())  # type: ignore[arg-type]

    mock_place.assert_not_called()
    # _liquidation_done 은 False 지만 STOPPING 진입은 됨 — 다음날 hang 방지.
    assert r._control_state is RunnerControlState.STOPPING


def test_intraday_momentum_fires_once_then_silent() -> None:
    """IntradayMomentum 은 첫 봉에 한 번만 BUY, 이후 NONE."""
    from auto_trading._common import Bar, Side

    strat = IntradayMomentum(weight=0.33)
    base = datetime.now(KST)
    bar1 = Bar(symbol="005930", ts=base, open=100, high=100, low=100, close=100, volume=1)
    bar2 = Bar(
        symbol="005930",
        ts=base + timedelta(minutes=1),
        open=100,
        high=100,
        low=100,
        close=100,
        volume=1,
    )

    sigs1 = strat.on_bar(bar1)
    sigs2 = strat.on_bar(bar2)

    assert len(sigs1) == 1
    assert sigs1[0].side is Side.BUY
    assert sigs1[0].weight == 0.33
    assert sigs1[0].symbol == "005930"
    assert sigs2 == []  # 두 번째 봉은 silent

    strat.reset()
    sigs3 = strat.on_bar(bar2)
    assert len(sigs3) == 1  # reset 후 다시 발사
