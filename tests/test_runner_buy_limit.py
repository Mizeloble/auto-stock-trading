"""runner 의 max_buys_per_symbol 한도(BUY 차단) 회귀.

검증:
- limits.max_buys_per_symbol > 0 + 카운트 한도 도달 → _evaluate_signal 이 None.
- 한도 미달이면 risk.evaluate 까지 정상 통과.
- 한도 비활성(0) 이면 카운터 무관하게 통과.
- SELL 시그널은 카운터·한도 영향 없이 risk.evaluate 도달.
- BUY 체결 시 카운터 누적 (place_and_track 경로).
- dry_run BUY 도 같은 카운터 증가 (한도 검증 회로 동일).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from auto_trading._common import KST, Bar, Side
from auto_trading.account.orders import Order, OrderResult, OrderType
from auto_trading.risk.manager import RiskLimits, RiskManager
from auto_trading.runner import PaperRunner
from auto_trading.runner import paper as runner_module
from auto_trading.strategy.base import Signal
from auto_trading.strategy.sma_crossover import SmaCrossover


def _runner(limit: int = 2, *, dry_run: bool = False) -> PaperRunner:
    return PaperRunner(
        strategy=SmaCrossover(),
        symbols=["005930"],
        risk=RiskManager(limits=RiskLimits(max_buys_per_symbol=limit)),
        dry_run=dry_run,
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


def _account() -> runner_module.AccountState:
    return runner_module.AccountState(
        deposit=10_000_000,
        total_equity=10_000_000,
        positions={},
        avg_prices={},
    )


async def test_buy_blocked_when_daily_count_reaches_limit() -> None:
    r = _runner(limit=2)
    # 이미 같은 종목 2회 매수 카운트가 누적된 상태로 강제.
    r._roll_daily_buy_counts()
    r._daily_buy_counts["005930"] = 2

    signal = Signal(symbol="005930", side=Side.BUY, weight=0.1, price_hint=Decimal("70000"))
    order = await r._evaluate_signal(signal, _bar(), _account())
    assert order is None


async def test_buy_allowed_when_below_limit() -> None:
    r = _runner(limit=3)
    r._roll_daily_buy_counts()
    r._daily_buy_counts["005930"] = 2  # 1회 더 가능

    signal = Signal(symbol="005930", side=Side.BUY, weight=0.05, price_hint=Decimal("70000"))
    with patch.object(r.risk, "evaluate", wraps=r.risk.evaluate) as spy:
        await r._evaluate_signal(signal, _bar(), _account())
    spy.assert_called_once()  # 한도 가드 통과 → risk.evaluate 까지 도달


async def test_buy_limit_disabled_when_zero() -> None:
    """max_buys_per_symbol=0 (기본/비활성) — 카운트 누적돼도 차단하지 않는다."""
    r = _runner(limit=0)
    r._daily_buy_counts["005930"] = 99  # 비활성이므로 무시되어야 함.

    signal = Signal(symbol="005930", side=Side.BUY, weight=0.05, price_hint=Decimal("70000"))
    with patch.object(r.risk, "evaluate", wraps=r.risk.evaluate) as spy:
        await r._evaluate_signal(signal, _bar(), _account())
    spy.assert_called_once()


async def test_sell_unaffected_by_buy_limit() -> None:
    """SELL 시그널은 BUY 카운트와 무관하게 risk.evaluate 까지 도달."""
    r = _runner(limit=1)
    r._roll_daily_buy_counts()
    r._daily_buy_counts["005930"] = 5  # 한도 초과 상태

    sell = Signal(symbol="005930", side=Side.SELL, weight=1.0, price_hint=Decimal("70000"))
    account = runner_module.AccountState(
        deposit=10_000_000,
        total_equity=10_000_000,
        positions={"005930": 3},
        avg_prices={"005930": 70_000.0},
    )
    with patch.object(r.risk, "evaluate", wraps=r.risk.evaluate) as spy:
        await r._evaluate_signal(sell, _bar(), account)
    spy.assert_called_once()


def _runner_killswitch() -> PaperRunner:
    """daily_loss_kill=True 러너 — 평가금액 기준이 없으면 무방비(fail-safe) 차단."""
    return PaperRunner(
        strategy=SmaCrossover(),
        symbols=["005930"],
        risk=RiskManager(limits=RiskLimits(daily_loss_kill=True, daily_loss_limit=0.03)),
    )


def _account_with_baseline() -> runner_module.AccountState:
    """day_start_equity 가 잡힌 정상(무방비 아님) 계좌."""
    return runner_module.AccountState(
        deposit=10_000_000,
        total_equity=10_000_000,
        positions={},
        avg_prices={},
        day_start_equity=10_000_000,
    )


async def test_buy_blocked_when_kill_switch_blind() -> None:
    """fail-safe — daily_loss_kill=True 인데 day_start_equity=0(무방비) → 새 매수 자동 차단,
    risk.evaluate 도달 전에 None.
    """
    r = _runner_killswitch()
    signal = Signal(symbol="005930", side=Side.BUY, weight=0.05, price_hint=Decimal("70000"))
    with patch.object(r.risk, "evaluate", wraps=r.risk.evaluate) as spy:
        out = await r._evaluate_signal(signal, _bar(), _account())  # day_start_equity=0
    assert out is None
    spy.assert_not_called()  # 무방비면 평가 자체에 도달하지 않음


async def test_buy_allowed_when_baseline_present() -> None:
    """평가금액 기준이 있으면 fail-safe 차단 없음 — 정상 평가 경로."""
    r = _runner_killswitch()
    signal = Signal(symbol="005930", side=Side.BUY, weight=0.05, price_hint=Decimal("70000"))
    with patch.object(r.risk, "evaluate", wraps=r.risk.evaluate) as spy:
        await r._evaluate_signal(signal, _bar(), _account_with_baseline())
    spy.assert_called_once()


async def test_sell_unaffected_when_kill_switch_blind() -> None:
    """무방비여도 SELL(보유분 청산)은 차단되지 않는다 — 손실 방어 보존."""
    r = _runner_killswitch()
    sell = Signal(symbol="005930", side=Side.SELL, weight=1.0, price_hint=Decimal("70000"))
    account = runner_module.AccountState(
        deposit=10_000_000,
        total_equity=10_000_000,
        positions={"005930": 3},
        avg_prices={"005930": 70_000.0},
        day_start_equity=0.0,  # 무방비
    )
    with patch.object(r.risk, "evaluate", wraps=r.risk.evaluate) as spy:
        await r._evaluate_signal(sell, _bar(), account)
    spy.assert_called_once()  # SELL 은 평가 도달


# ---- churn 브레이크 (daily_realized_loss_limit) -----------------------------


def _runner_loss_limit(limit_won: float) -> PaperRunner:
    return PaperRunner(
        strategy=SmaCrossover(),
        symbols=["005930"],
        risk=RiskManager(limits=RiskLimits(daily_realized_loss_limit=limit_won)),
    )


async def test_daily_realized_loss_accumulates_net_on_sell() -> None:
    """SELL 체결 시 (체결가-평단가)*수량 - 왕복비용 이 _daily_realized_net 에 적립(NET)."""
    r = _runner_loss_limit(0)  # 적립은 한도 비활성이어도 동작
    account = runner_module.AccountState(
        deposit=0,
        total_equity=700_000,
        positions={"005930": 10},
        avg_prices={"005930": 70_000.0},
    )
    order = Order(
        symbol="005930",
        side=Side.SELL,
        quantity=10,
        order_type=OrderType.MARKET,
        price=Decimal("68000"),  # ref_price = 68,000 → gross (68000-70000)*10 = -20,000
    )
    with patch(
        "auto_trading.runner.paper.place_order",
        new=AsyncMock(return_value=OrderResult(order_no="S1", client_id="c1", raw={})),
    ):
        await r._place_and_track(rest=object(), order=order, bar=_bar(), account=account)  # type: ignore[arg-type]
    # 비용 = 700,000*(0.00015+0.0005) + 680,000*(0.00015+0.002+0.0005) = 455 + 1,802 = 2,257
    # NET = -20,000 - 2,257 = -22,257
    assert r._daily_realized_net == pytest.approx(-22_257.0)


async def test_buy_blocked_when_daily_realized_loss_limit_reached() -> None:
    """그날 누적 실현손실이 한도 이상이면 신규 BUY 차단 (risk.evaluate 도달 전)."""
    r = _runner_loss_limit(100_000)
    r._daily_buy_counts_date = datetime.now(KST).date()  # 오늘자 — roll 이 누적을 리셋 않게
    r._daily_realized_net = -120_000.0  # 12만 손실 (한도 10만 초과)
    signal = Signal(symbol="005930", side=Side.BUY, weight=0.05, price_hint=Decimal("70000"))
    with patch.object(r.risk, "evaluate", wraps=r.risk.evaluate) as spy:
        out = await r._evaluate_signal(signal, _bar(), _account())
    assert out is None
    spy.assert_not_called()


async def test_buy_allowed_when_loss_below_limit() -> None:
    """누적 손실이 한도 미만이면 정상 통과."""
    r = _runner_loss_limit(100_000)
    r._daily_buy_counts_date = datetime.now(KST).date()
    r._daily_realized_net = -50_000.0  # 한도 10만 미달
    signal = Signal(symbol="005930", side=Side.BUY, weight=0.05, price_hint=Decimal("70000"))
    with patch.object(r.risk, "evaluate", wraps=r.risk.evaluate) as spy:
        await r._evaluate_signal(signal, _bar(), _account())
    spy.assert_called_once()


async def test_daily_loss_limit_disabled_when_zero() -> None:
    """daily_realized_loss_limit=0 (기본/비활성) — 큰 손실 누적돼도 차단하지 않는다."""
    r = _runner_loss_limit(0)
    r._daily_realized_net = -9_999_999.0
    signal = Signal(symbol="005930", side=Side.BUY, weight=0.05, price_hint=Decimal("70000"))
    with patch.object(r.risk, "evaluate", wraps=r.risk.evaluate) as spy:
        await r._evaluate_signal(signal, _bar(), _account())
    spy.assert_called_once()


async def test_sell_unaffected_by_daily_loss_limit() -> None:
    """한도 초과 상태라도 SELL(청산)은 차단되지 않는다 — 손실 방어 보존."""
    r = _runner_loss_limit(100_000)
    r._daily_realized_net = -500_000.0  # 한도 한참 초과
    sell = Signal(symbol="005930", side=Side.SELL, weight=1.0, price_hint=Decimal("70000"))
    account = runner_module.AccountState(
        deposit=10_000_000,
        total_equity=10_000_000,
        positions={"005930": 3},
        avg_prices={"005930": 70_000.0},
    )
    with patch.object(r.risk, "evaluate", wraps=r.risk.evaluate) as spy:
        await r._evaluate_signal(sell, _bar(), account)
    spy.assert_called_once()


async def test_daily_realized_net_resets_on_new_day() -> None:
    """일자 경계를 넘으면 누적 실현손익이 0 으로 리셋."""
    from datetime import date

    r = _runner_loss_limit(100_000)
    r._daily_realized_net = -120_000.0
    r._daily_buy_counts_date = date(2020, 1, 1)  # 과거 → roll 시 리셋
    r._roll_daily_buy_counts()
    assert r._daily_realized_net == 0.0


async def test_buy_count_incremented_on_successful_fill() -> None:
    """_place_and_track 가 BUY 성공 시 카운터를 1 증가."""
    r = _runner(limit=3)
    order = Order(
        symbol="005930",
        side=Side.BUY,
        quantity=10,
        order_type=OrderType.MARKET,
        price=Decimal("70000"),
    )
    with patch(
        "auto_trading.runner.paper.place_order",
        new=AsyncMock(return_value=OrderResult(order_no="X1", client_id="c1", raw={})),
    ):
        await r._place_and_track(rest=object(), order=order, bar=_bar(), account=_account())  # type: ignore[arg-type]
    assert r._daily_buy_counts["005930"] == 1


async def test_dry_run_buy_also_increments_counter() -> None:
    """dry_run BUY 도 카운터를 누적 — 한도 검증 회로가 라이브와 동일."""
    r = _runner(limit=2, dry_run=True)
    signal = Signal(symbol="005930", side=Side.BUY, weight=0.05, price_hint=Decimal("70000"))
    # _evaluate_signal 가 dry-run 분기에서 None 반환 + 카운터 증가.
    out = await r._evaluate_signal(signal, _bar(), _account())
    assert out is None  # dry-run 은 실주문 없음
    assert r._daily_buy_counts["005930"] == 1
    # 두 번째도 한도 내라 OK.
    await r._evaluate_signal(signal, _bar(), _account())
    assert r._daily_buy_counts["005930"] == 2
    # 세 번째는 한도 초과로 차단 (카운터 더 안 증가).
    await r._evaluate_signal(signal, _bar(), _account())
    assert r._daily_buy_counts["005930"] == 2
