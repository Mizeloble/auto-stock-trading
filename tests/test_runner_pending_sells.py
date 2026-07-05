"""5/14 회귀 — SELL 발주 후 키움 잔고 응답에 체결이 반영되기까지의 race 차단.

검증:
- SELL 발주 성공 시 _pending_sells 에 수량 등록.
- 다음 _snapshot_account 호출에서 raw 잔고의 positions 가 그만큼 차감됨 (가드·전략이
  잔량을 부풀려 보고 또 SELL 시도하는 800033 insufficient_balance 패턴 차단).
- BUY 발주는 _pending_sells 에 영향 X.
- SELL 발주 실패(except) 는 등록 X.
- TTL 지난 엔트리는 _reap_pending_sells() 로 만료.
- runner.order_failed 가 insufficient_balance + SELL 이면 텔레그램 알림 생략(silenced),
  그 외는 정상 알림.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from auto_trading._common import KST, Bar, Side
from auto_trading.account.balance import AccountSnapshot, Holding
from auto_trading.account.orders import Order, OrderResult, OrderType
from auto_trading.kiwoom.schemas import KiwoomError
from auto_trading.risk.manager import RiskLimits, RiskManager
from auto_trading.runner import PaperRunner
from auto_trading.runner import paper as runner_module
from auto_trading.strategy.sma_crossover import SmaCrossover


def _runner() -> PaperRunner:
    return PaperRunner(strategy=SmaCrossover(), symbols=["005930"])


def _guard_runner() -> PaperRunner:
    """stop_loss 활성 — PositionGuard 가 발화 가능한 상태."""
    return PaperRunner(
        strategy=SmaCrossover(),
        symbols=["005930"],
        risk=RiskManager(limits=RiskLimits(stop_loss_pct=0.01)),
    )


def _bar(symbol: str = "005930", close: float = 70_000.0) -> Bar:
    return Bar(
        symbol=symbol,
        ts=datetime.now(KST),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1_000,
    )


def _account(positions: dict[str, int] | None = None) -> runner_module.AccountState:
    return runner_module.AccountState(
        deposit=10_000_000,
        total_equity=10_000_000,
        positions=positions or {"005930": 89},
        avg_prices={"005930": 70_000.0},
    )


def _snapshot(qty: int) -> AccountSnapshot:
    return AccountSnapshot(
        deposit=Decimal("10000000"),
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


# ---- SELL 발주 성공 → pending 등록 -------------------------------------------


async def test_successful_sell_registers_pending_quantity() -> None:
    r = _runner()
    r.notifier = AsyncMock()
    order = Order(symbol="005930", side=Side.SELL, quantity=89, order_type=OrderType.MARKET)

    with patch.object(
        runner_module,
        "place_order",
        AsyncMock(return_value=OrderResult(order_no="X1", raw={}, client_id="c")),
    ):
        await r._place_and_track(rest=object(), order=order, bar=_bar(), account=_account())  # type: ignore[arg-type]

    qty, _ts = r._pending_sells["005930"]
    assert qty == 89


async def test_successful_buy_does_not_register_pending() -> None:
    r = _runner()
    r.notifier = AsyncMock()
    order = Order(symbol="005930", side=Side.BUY, quantity=5, order_type=OrderType.MARKET)

    with patch.object(
        runner_module,
        "place_order",
        AsyncMock(return_value=OrderResult(order_no="X2", raw={}, client_id="c")),
    ):
        await r._place_and_track(rest=object(), order=order, bar=_bar(), account=_account())  # type: ignore[arg-type]

    assert "005930" not in r._pending_sells


async def test_failed_sell_does_not_register_pending() -> None:
    r = _runner()
    r.notifier = AsyncMock()
    order = Order(symbol="005930", side=Side.SELL, quantity=89, order_type=OrderType.MARKET)

    with patch.object(
        runner_module,
        "place_order",
        AsyncMock(side_effect=KiwoomError(code="0", message="예수금 부족")),
    ):
        await r._place_and_track(rest=object(), order=order, bar=_bar(), account=_account())  # type: ignore[arg-type]

    assert "005930" not in r._pending_sells


# ---- _snapshot_account 에서 raw 잔고 차감 -------------------------------------


async def test_snapshot_account_subtracts_pending_sells() -> None:
    """SELL 89주 발주 후 키움이 미반영 89주를 그대로 응답하면 positions 0 으로 보정."""
    r = _runner()
    r.notifier = AsyncMock()
    # 직접 pending 등록 (위 테스트가 발주 흐름은 검증).
    r._pending_sells["005930"] = (89, datetime.now())

    with patch.object(
        runner_module,
        "get_balance",
        AsyncMock(return_value=_snapshot(qty=89)),
    ):
        state = await r._snapshot_account(rest=object(), previous=None)  # type: ignore[arg-type]

    assert state.positions["005930"] == 0


async def test_snapshot_account_does_not_go_negative() -> None:
    """청산이 일부만 반영된 응답(예: 89 → 50) 에서도 max(0, raw - pending) 로 안전."""
    r = _runner()
    r.notifier = AsyncMock()
    r._pending_sells["005930"] = (89, datetime.now())

    with patch.object(
        runner_module,
        "get_balance",
        AsyncMock(return_value=_snapshot(qty=50)),
    ):
        state = await r._snapshot_account(rest=object(), previous=None)  # type: ignore[arg-type]

    assert state.positions["005930"] == 0


def test_reap_pending_sells_drops_expired_entries() -> None:
    """_pending_sell_ttl_sec 지난 엔트리는 자연 만료 — 키움 응답을 다시 신뢰."""
    r = _runner()
    r._pending_sells["005930"] = (
        10,
        datetime.now() - timedelta(seconds=r._pending_sell_ttl_sec + 5),
    )
    r._pending_sells["000660"] = (5, datetime.now())  # 최근 — 유지

    r._reap_pending_sells()

    assert "005930" not in r._pending_sells
    assert r._pending_sells["000660"][0] == 5


# ---- INSUFFICIENT_BALANCE 알림 강등 -------------------------------------------


async def test_sell_insufficient_balance_silences_notify() -> None:
    """5/14: in-flight race 로 인한 800033 SELL 실패는 텔레그램 노이즈 — 알림 생략."""
    r = _runner()
    fake_notifier = AsyncMock()
    r.notifier = fake_notifier
    order = Order(symbol="005930", side=Side.SELL, quantity=44, order_type=OrderType.MARKET)

    with patch.object(
        runner_module,
        "place_order",
        AsyncMock(side_effect=KiwoomError(code="20", message="모의투자 매도가능수량이 부족합니다")),
    ):
        await r._place_and_track(rest=object(), order=order, bar=_bar(), account=_account())  # type: ignore[arg-type]

    fake_notifier.notify.assert_not_awaited()


async def test_buy_insufficient_balance_still_notifies() -> None:
    """BUY 측 잔고부족(예수금 부족) 은 사용자에게 즉시 알려야 함 — 강등 X."""
    r = _runner()
    fake_notifier = AsyncMock()
    r.notifier = fake_notifier
    order = Order(symbol="005930", side=Side.BUY, quantity=10, order_type=OrderType.MARKET)

    with patch.object(
        runner_module,
        "place_order",
        AsyncMock(side_effect=KiwoomError(code="0", message="예수금 부족")),
    ):
        await r._place_and_track(rest=object(), order=order, bar=_bar(), account=_account())  # type: ignore[arg-type]

    fake_notifier.notify.assert_awaited_once()
    args, _ = fake_notifier.notify.call_args
    assert args[0] == "runner.order_failed"


# ---- 5/14 회귀 후속(2026-05-16): 미체결 SELL 이 잔량 전체를 덮으면 guard 발화 차단 ----


async def test_position_guard_skipped_when_pending_sell_covers_position() -> None:
    """89주 보유 + 미체결 SELL 89주 → stop_loss 조건이어도 재청산 발주 안 함."""
    r = _guard_runner()
    r.notifier = AsyncMock()
    r._pending_sells["005930"] = (89, datetime.now())
    placed = AsyncMock()
    with patch.object(r, "_place_guard_action", placed):
        # close 60,000 (avg 70,000 대비 -14%) → stop_loss 조건이지만 net=0.
        await r._run_position_guard(
            rest=object(),  # type: ignore[arg-type]
            bar=_bar(close=60_000.0),
            account=_account({"005930": 89}),
        )
    placed.assert_not_awaited()


async def test_position_guard_fires_on_net_uncovered_quantity() -> None:
    """89주 보유 + 미체결 SELL 40주 → 잔여 49주에 대해 정상 발화."""
    r = _guard_runner()
    r.notifier = AsyncMock()
    r._pending_sells["005930"] = (40, datetime.now())
    placed = AsyncMock()
    with patch.object(r, "_place_guard_action", placed):
        await r._run_position_guard(
            rest=object(),  # type: ignore[arg-type]
            bar=_bar(close=60_000.0),
            account=_account({"005930": 89}),
        )
    placed.assert_awaited_once()
    action = placed.call_args.args[1]
    assert action.quantity == 49  # stop_loss = 전량(=net 49)


async def test_buy_accept_clears_pending_sells_for_symbol() -> None:
    """재진입 — 같은 종목 BUY 체결 시 직전 라운드트립의 stale pending 폐기."""
    r = _runner()
    r.notifier = AsyncMock()
    r._pending_sells["005930"] = (89, datetime.now())
    order = Order(symbol="005930", side=Side.BUY, quantity=50, order_type=OrderType.MARKET)
    with patch.object(
        runner_module,
        "place_order",
        AsyncMock(return_value=OrderResult(order_no="B1", raw={}, client_id="c")),
    ):
        await r._place_and_track(rest=object(), order=order, bar=_bar(), account=_account())  # type: ignore[arg-type]
    assert "005930" not in r._pending_sells
