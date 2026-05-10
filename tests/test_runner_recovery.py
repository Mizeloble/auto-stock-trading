"""runner 의 주문 저널 통합 회귀.

29단계 PR2 (#9 2차).

검증:
- journal 미주입 — `_warn_recovery_pending` 은 noop, notifier 호출 X.
- journal 주입 + 빈 저널 — notify 호출 X.
- journal 주입 + suspect 있음 — `runner.recovery_pending` notify 발송, samples 마스킹 (앞 8자).
- `_place_and_track` 가 place_order 에 journal 인자를 전달.
- liquidate_all 도 place_order 에 journal 인자를 전달.
"""

from __future__ import annotations

from datetime import datetime, time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import auto_trading.runner as runner_module
from auto_trading._common import Bar, Side
from auto_trading.account.order_journal import OrderJournal
from auto_trading.account.orders import Order, OrderResult, OrderType
from auto_trading.runner import PaperRunner
from auto_trading.strategy.sma_crossover import SmaCrossover


def _runner(*, journal: OrderJournal | None = None, day_trade: bool = False) -> PaperRunner:
    if day_trade:
        return PaperRunner(
            liquidate_at=time(15, 15),
            journal=journal,
        )
    return PaperRunner(
        strategy=SmaCrossover(),
        symbols=["005930"],
        journal=journal,
    )


def _account_state() -> runner_module.AccountState:
    return runner_module.AccountState(
        deposit=1_000_000,
        total_equity=1_000_000,
        positions={"005930": 5},
        avg_prices={"005930": 70_000.0},
    )


async def test_warn_recovery_pending_noop_when_journal_missing() -> None:
    r = _runner(journal=None)
    fake_notifier = AsyncMock()
    r.notifier = fake_notifier

    await r._warn_recovery_pending()

    fake_notifier.notify.assert_not_called()


async def test_warn_recovery_pending_silent_when_no_suspects(tmp_path: Path) -> None:
    journal = OrderJournal(path=tmp_path / "state.jsonl")
    journal.submit(client_id="c1", order=Order(symbol="005930", side=Side.BUY, quantity=1))
    journal.accepted(client_id="c1", order_no="OK")

    r = _runner(journal=journal)
    fake_notifier = AsyncMock()
    r.notifier = fake_notifier

    await r._warn_recovery_pending()

    fake_notifier.notify.assert_not_called()


async def test_warn_recovery_pending_notifies_with_masked_samples(tmp_path: Path) -> None:
    journal = OrderJournal(path=tmp_path / "state.jsonl")
    full_cid = "abcdef0123456789abcdef0123456789"
    journal.submit(
        client_id=full_cid,
        order=Order(symbol="005930", side=Side.BUY, quantity=10),
    )
    # accepted/failed 없음 → suspect

    r = _runner(journal=journal)
    fake_notifier = AsyncMock()
    r.notifier = fake_notifier

    await r._warn_recovery_pending()

    fake_notifier.notify.assert_awaited_once()
    args, kwargs = fake_notifier.notify.call_args
    assert args[0] == "runner.recovery_pending"
    assert kwargs["count"] == 1
    [sample] = kwargs["samples"]
    # 앞 8자만 노출 — 전체 cid 가 새지 않게.
    assert sample.startswith("abcdef01 ")
    assert full_cid not in sample
    assert "buy" in sample
    assert "005930" in sample
    assert "x10" in sample


async def test_place_and_track_passes_journal_to_place_order(tmp_path: Path) -> None:
    """_place_and_track 호출 시 place_order 가 journal=runner.journal 로 받는지."""
    journal = OrderJournal(path=tmp_path / "state.jsonl")
    r = _runner(journal=journal)

    fake_result = OrderResult(order_no="X1", raw={}, client_id="cid")
    bar = Bar(
        symbol="005930",
        ts=datetime.now(),
        open=70_000.0,
        high=70_500.0,
        low=69_800.0,
        close=70_200.0,
        volume=1_000,
    )
    order = Order(symbol="005930", side=Side.BUY, quantity=1, order_type=OrderType.MARKET)

    with patch.object(
        runner_module, "place_order", AsyncMock(return_value=fake_result)
    ) as mock_place:
        await r._place_and_track(rest=object(), order=order, bar=bar, account=_account_state())  # type: ignore[arg-type]

    assert mock_place.await_count == 1
    _, kwargs = mock_place.call_args
    assert kwargs.get("journal") is journal


async def test_liquidate_all_passes_journal_to_place_order(tmp_path: Path) -> None:
    """liquidate_all 의 place_order 호출도 journal 인자를 전달."""
    journal = OrderJournal(path=tmp_path / "state.jsonl")
    r = _runner(journal=journal, day_trade=True)
    account = _account_state()

    captured_kwargs: list[dict[str, object]] = []

    async def _fake_place(rest, order, **kwargs):  # type: ignore[no-untyped-def]
        captured_kwargs.append(kwargs)
        return OrderResult(order_no=f"ORD-{order.symbol}", raw={}, client_id="x")

    with patch.object(runner_module, "place_order", AsyncMock(side_effect=_fake_place)):
        await r.liquidate_all(rest=object(), account=account, reason="test")  # type: ignore[arg-type]

    assert captured_kwargs, "place_order 가 한 번 이상 호출되어야 함"
    for kwargs in captured_kwargs:
        assert kwargs.get("journal") is journal


async def test_runner_journal_default_is_none() -> None:
    """필드 추가가 기존 생성자 호환을 깨지 않는지 — journal 미지정 시 None."""
    r = PaperRunner(strategy=SmaCrossover(), symbols=["005930"])
    assert r.journal is None


pytestmark = pytest.mark.asyncio
