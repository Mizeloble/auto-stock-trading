"""2026-06-10 제3자 검토 후속 — 보호장치가 tick 없이도 도는지 + BUY 잔고 race 보정.

검증:
- _protection_loop: tick 루프와 무관하게 잔고 갱신 + kill switch 발화 → STOPPING.
- _protection_loop: 잔고 fetch 연속 실패(fail-loud) → request_stop 으로 정지.
- _pending_buys: BUY 접수 후 키움 잔고가 체결 미반영이어도 로컬 기대 수량 유지
  (보호 공백·중복 비중 사용 차단). raw 반영 시 해제, SELL 접수 시 폐기.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import auto_trading.runner as runner_module
from auto_trading._common import KST, Bar, Side
from auto_trading.account.balance import AccountSnapshot, Holding
from auto_trading.account.orders import Order, OrderResult, OrderType
from auto_trading.risk.manager import AccountState, RiskLimits, RiskManager
from auto_trading.runner import PaperRunner, RunnerControlState
from auto_trading.strategy.sma_crossover import SmaCrossover


def _runner(**kwargs) -> PaperRunner:
    return PaperRunner(strategy=SmaCrossover(), symbols=["005930"], **kwargs)


def _bar(close: float = 70_000.0) -> Bar:
    return Bar(
        symbol="005930",
        ts=datetime.now(KST),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=100,
    )


def _snapshot(qty: int, *, deposit: int = 10_000_000) -> AccountSnapshot:
    return AccountSnapshot(
        deposit=Decimal(deposit),
        total_evaluation=Decimal("0"),
        total_pnl=Decimal("0"),
        holdings=[
            Holding(
                symbol="005930",
                name="삼성전자",
                quantity=qty,
                avg_price=Decimal("70000"),
                current_price=Decimal("70000"),
                market_value=Decimal(str(qty * 70_000)),
                pnl=Decimal("0"),
                pnl_pct=Decimal("0"),
            )
        ]
        if qty > 0
        else [],
    )


# ---- _protection_loop — tick 없이 kill switch ---------------------------------


async def test_protection_loop_fires_kill_switch_without_ticks() -> None:
    """시세(tick)가 한 건도 없어도 잔고 갱신 + daily_loss_kill 강제청산이 돈다."""
    r = _runner(
        risk=RiskManager(limits=RiskLimits(daily_loss_limit=0.03, daily_loss_kill=True)),
        balance_refresh_sec=0,  # 테스트 — 즉시 갱신 주기
        dry_run=True,  # liquidate_all 이 실주문 없이 시뮬
    )
    r.notifier = AsyncMock()
    # 직전 스냅샷 — baseline 10M. 이후 protection loop 가 받는 잔고는 -4% (한도 -3% 초과).
    r._account = AccountState(
        deposit=9_600_000,
        total_equity=9_600_000,
        positions={"005930": 10},
        avg_prices={"005930": 70_000.0},
        day_start_equity=10_000_000,
    )

    with patch.object(
        runner_module, "get_balance", AsyncMock(return_value=_snapshot(10, deposit=8_900_000))
    ):
        task = asyncio.create_task(r._protection_loop(rest=object()))  # type: ignore[arg-type]
        await asyncio.wait_for(task, timeout=2.0)  # kill switch → request_stop → 루프 종료

    assert r._control_state is RunnerControlState.STOPPING
    assert r._liquidation_done is True
    events = [c.args[0] for c in r.notifier.notify.call_args_list]
    assert "runner.kill_switch_triggered" in events


async def test_protection_loop_stops_runner_on_fatal_balance_failure() -> None:
    """잔고 연속 실패 한도 초과(fail-loud) — 메인 루프가 죽던 기존 동작처럼 정지."""
    r = _runner(balance_refresh_sec=0, max_balance_failures=1)
    r.notifier = AsyncMock()
    r._account = AccountState(deposit=1, total_equity=1, day_start_equity=1)

    with patch.object(
        runner_module, "get_balance", AsyncMock(side_effect=RuntimeError("kiwoom down"))
    ):
        task = asyncio.create_task(r._protection_loop(rest=object()))  # type: ignore[arg-type]
        await asyncio.wait_for(task, timeout=2.0)

    assert r._control_state is RunnerControlState.STOPPING


async def test_protection_loop_exits_quietly_on_stopping() -> None:
    """STOPPING 진입 후에는 갱신 없이 즉시 종료."""
    r = _runner(balance_refresh_sec=0)
    r.request_stop()
    with patch.object(runner_module, "get_balance", AsyncMock()) as mock_bal:
        await asyncio.wait_for(r._protection_loop(rest=object()), timeout=1.0)  # type: ignore[arg-type]
    mock_bal.assert_not_called()


# ---- _pending_buys — BUY 잔고 race 보정 ---------------------------------------


async def test_buy_registers_pending_and_snapshot_keeps_local_quantity() -> None:
    """BUY 접수 직후 키움이 체결 미반영(보유 0) 응답을 줘도 로컬 기대 수량 유지."""
    r = _runner()
    r.notifier = AsyncMock()
    account = AccountState(deposit=10_000_000, total_equity=10_000_000, day_start_equity=10_000_000)
    order = Order(symbol="005930", side=Side.BUY, quantity=5, order_type=OrderType.MARKET)

    with patch.object(
        runner_module,
        "place_order",
        AsyncMock(return_value=OrderResult(order_no="B1", raw={}, client_id="c")),
    ):
        await r._place_and_track(rest=object(), order=order, bar=_bar(), account=account)  # type: ignore[arg-type]

    qty, _ts = r._pending_buys["005930"]
    assert qty == 5

    # 키움 잔고가 아직 0 주 응답 → 보정으로 5 주 유지 + 평단도 로컬 값 유지(guard 보호).
    with patch.object(runner_module, "get_balance", AsyncMock(return_value=_snapshot(0))):
        state = await r._snapshot_account(rest=object(), previous=account)  # type: ignore[arg-type]
    assert state.positions["005930"] == 5
    assert state.avg_prices["005930"] == 70_000.0


async def test_pending_buy_cleared_when_balance_reflects_fill() -> None:
    """키움 raw 가 기대 수량에 도달하면 보정 해제 — 이후 응답을 그대로 신뢰."""
    r = _runner()
    r._pending_buys["005930"] = (5, datetime.now())
    prev = AccountState(deposit=0, total_equity=0, day_start_equity=1)

    with patch.object(runner_module, "get_balance", AsyncMock(return_value=_snapshot(5))):
        state = await r._snapshot_account(rest=object(), previous=prev)  # type: ignore[arg-type]

    assert state.positions["005930"] == 5
    assert "005930" not in r._pending_buys


async def test_sell_accept_discards_pending_buy() -> None:
    """SELL 접수 시 BUY 기대치는 폐기 — stale 보정이 청산된 포지션을 되살리지 않게."""
    r = _runner()
    r.notifier = AsyncMock()
    r._pending_buys["005930"] = (5, datetime.now())
    account = AccountState(
        deposit=0,
        total_equity=350_000,
        positions={"005930": 5},
        avg_prices={"005930": 70_000.0},
        day_start_equity=350_000,
    )
    order = Order(symbol="005930", side=Side.SELL, quantity=5, order_type=OrderType.MARKET)

    with patch.object(
        runner_module,
        "place_order",
        AsyncMock(return_value=OrderResult(order_no="S1", raw={}, client_id="c")),
    ):
        await r._place_and_track(rest=object(), order=order, bar=_bar(), account=account)  # type: ignore[arg-type]

    assert "005930" not in r._pending_buys


async def test_pending_buy_expires_after_ttl() -> None:
    """TTL 만료 후엔 키움 응답을 다시 신뢰 (영구 보정 고착 방지)."""
    r = _runner()
    r._pending_buys["005930"] = (
        5,
        datetime.now() - timedelta(seconds=r._pending_sell_ttl_sec + 1),
    )
    prev = AccountState(deposit=0, total_equity=0, day_start_equity=1)

    with patch.object(runner_module, "get_balance", AsyncMock(return_value=_snapshot(0))):
        state = await r._snapshot_account(rest=object(), previous=prev)  # type: ignore[arg-type]

    assert state.positions.get("005930", 0) == 0
    assert "005930" not in r._pending_buys
