"""5/14 사용자 피드백 — 텔레그램 메시지에서 종목 코드 옆에 종목명 합쳐 표시.

PaperRunner 의 `_symbol_names` 가 채워지는 모든 경로 + state_snapshot 노출 검증:
- `register_symbol_names()` 직접 주입
- `set_universe(..., names=…)` 발굴 결과 주입
- `_snapshot_account()` 의 raw 잔고 `h.name` 자동 흡수
- 빈/공백 이름은 무시 (코드만 표시)
- 알림 payload 에 `name=` 이 들어가는지 (order_accepted/failed, position_guard.fired,
  liquidate_failed, runner.started)
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import auto_trading.runner as runner_module
from auto_trading._common import KST, Bar, Side
from auto_trading.account.balance import AccountSnapshot, Holding
from auto_trading.account.orders import Order, OrderResult, OrderType
from auto_trading.kiwoom.schemas import KiwoomError
from auto_trading.runner import PaperRunner
from auto_trading.strategy.intraday import IntradayMomentum
from auto_trading.strategy.sma_crossover import SmaCrossover


def _runner() -> PaperRunner:
    return PaperRunner(strategy=SmaCrossover(), symbols=["005930"])


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


def _account() -> runner_module.AccountState:
    return runner_module.AccountState(
        deposit=10_000_000,
        total_equity=10_000_000,
        positions={"005930": 5},
        avg_prices={"005930": 70_000.0},
    )


def _snapshot_with_name(symbol: str, name: str, qty: int = 5) -> AccountSnapshot:
    return AccountSnapshot(
        deposit=Decimal("10000000"),
        total_evaluation=Decimal("350000"),
        total_pnl=Decimal("0"),
        holdings=[
            Holding(
                symbol=symbol,
                name=name,
                quantity=qty,
                avg_price=Decimal("70000"),
                current_price=Decimal("70000"),
                market_value=Decimal(str(qty * 70_000)),
                pnl=Decimal("0"),
                pnl_pct=Decimal("0"),
            )
        ],
    )


# ---- _symbol_names 채워지는 경로 -----------------------------------------------


def test_register_symbol_names_stores_valid_pairs() -> None:
    r = _runner()
    r.register_symbol_names({"005930": "삼성전자", "403550": "쏘카"})
    assert r._symbol_names == {"005930": "삼성전자", "403550": "쏘카"}


def test_register_symbol_names_ignores_empty_or_whitespace() -> None:
    r = _runner()
    r.register_symbol_names({"005930": "", "403550": "  ", "000660": "SK하이닉스"})
    assert r._symbol_names == {"000660": "SK하이닉스"}


def test_set_universe_absorbs_names_kwarg() -> None:
    """발굴 단계가 이름을 함께 넘기면 _symbol_names 에 등록."""
    from datetime import time

    # day-trade 모드 — 시동 시 universe 미정 허용. set_universe 가 채움.
    r = PaperRunner(liquidate_at=time(15, 15, tzinfo=KST))
    strat = IntradayMomentum(weight=1.0)
    r.set_universe(
        ["005930", "403550"],
        {"005930": strat, "403550": strat},
        names={"005930": "삼성전자", "403550": "쏘카"},
    )
    assert r._symbol_names["005930"] == "삼성전자"
    assert r._symbol_names["403550"] == "쏘카"


async def test_snapshot_account_absorbs_holdings_name() -> None:
    """잔고 응답의 holdings.name 을 자동 흡수."""
    r = _runner()
    with patch.object(
        runner_module,
        "get_balance",
        AsyncMock(return_value=_snapshot_with_name("005930", "삼성전자")),
    ):
        await r._snapshot_account(rest=object(), previous=None)  # type: ignore[arg-type]
    assert r._symbol_names.get("005930") == "삼성전자"


async def test_snapshot_account_does_not_clobber_existing_name() -> None:
    """잔고 응답 이름이 빈 값이어도 기존 등록 이름은 유지."""
    r = _runner()
    r.register_symbol_names({"005930": "삼성전자"})
    with patch.object(
        runner_module,
        "get_balance",
        AsyncMock(return_value=_snapshot_with_name("005930", "")),
    ):
        await r._snapshot_account(rest=object(), previous=None)  # type: ignore[arg-type]
    assert r._symbol_names["005930"] == "삼성전자"


# ---- state_snapshot 노출 -------------------------------------------------------


def test_state_snapshot_exposes_symbol_names() -> None:
    r = _runner()
    r.register_symbol_names({"005930": "삼성전자"})
    snap = r.state_snapshot()
    assert snap.symbol_names == {"005930": "삼성전자"}


# ---- 알림 payload 에 name 포함 ------------------------------------------------


async def test_order_accepted_notify_includes_name() -> None:
    r = _runner()
    r.register_symbol_names({"005930": "삼성전자"})
    fake = AsyncMock()
    r.notifier = fake
    order = Order(symbol="005930", side=Side.BUY, quantity=5, order_type=OrderType.MARKET)

    with patch.object(
        runner_module,
        "place_order",
        AsyncMock(return_value=OrderResult(order_no="X", raw={}, client_id="c")),
    ):
        await r._place_and_track(rest=object(), order=order, bar=_bar(), account=_account())  # type: ignore[arg-type]

    fake.notify.assert_awaited_once()
    _, kwargs = fake.notify.call_args
    assert kwargs["name"] == "삼성전자"


async def test_order_failed_notify_includes_name() -> None:
    r = _runner()
    r.register_symbol_names({"005930": "삼성전자"})
    fake = AsyncMock()
    r.notifier = fake
    # BUY 잔고부족 → silence 분기 아님(SELL 만 강등) → notify 발사.
    order = Order(symbol="005930", side=Side.BUY, quantity=10, order_type=OrderType.MARKET)

    with patch.object(
        runner_module,
        "place_order",
        AsyncMock(side_effect=KiwoomError(code="0", message="예수금 부족")),
    ):
        await r._place_and_track(rest=object(), order=order, bar=_bar(), account=_account())  # type: ignore[arg-type]

    _, kwargs = fake.notify.call_args
    assert kwargs["name"] == "삼성전자"


async def test_liquidate_failed_notify_includes_name() -> None:
    r = _runner()
    r.register_symbol_names({"005930": "삼성전자"})
    fake = AsyncMock()
    r.notifier = fake
    account = _account()

    with patch.object(
        runner_module,
        "place_order",
        AsyncMock(side_effect=KiwoomError(code="7", message="종목 정보가 없습니다")),
    ):
        await r.liquidate_all(rest=object(), account=account, reason="test")  # type: ignore[arg-type]

    failed_call = next(
        c for c in fake.notify.await_args_list if c.args and c.args[0] == "runner.liquidate_failed"
    )
    assert failed_call.kwargs["name"] == "삼성전자"
