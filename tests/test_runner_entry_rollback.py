"""BUY 거절/실패 시 전략 진입 기록 롤백 회귀 (2026-06-10 제3자 검토).

index_daytrade 류 전략은 시그널 발사 시점에 `state["last_entry_ts"]` 를 기록해
'보유 중'으로 간주한다. 리스크 거절·발주 실패로 주문이 안 나갔는데 그대로 두면
그 종목은 그날 내내 재진입 불가 — 다회 진입 설계가 거절 한 번에 무력화된다.

검증:
- 리스크 REJECT → last_entry_ts 롤백 + 다음 봉에서 재발사 가능.
- dry-run 시뮬 체결 → 롤백하지 않음 (시뮬 = 진입 성공).
- _place_and_track 발주 실패(except) → 롤백.
- 이미 청산 후 상태(last_exit > last_entry)면 롤백 no-op.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from auto_trading._common import KST, Bar, Side
from auto_trading.account.orders import Order, OrderType
from auto_trading.kiwoom.schemas import KiwoomError
from auto_trading.runner import PaperRunner
from auto_trading.runner import paper as runner_module
from auto_trading.strategy.index_daytrade import IndexMomentumDayTrade


def _runner(**kwargs) -> PaperRunner:
    return PaperRunner(
        strategy=IndexMomentumDayTrade(cooldown_min=0),
        symbols=["005930"],
        **kwargs,
    )


def _bar(close: float = 10_000.0, ts: datetime | None = None) -> Bar:
    return Bar(
        symbol="005930",
        ts=ts or datetime.now(KST),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=100,
    )


def _broke_account() -> runner_module.AccountState:
    """예수금 0 → 리스크 매니저가 BUY 를 budget 부족으로 거절."""
    return runner_module.AccountState(deposit=0, total_equity=0, day_start_equity=10_000_000)


def _rich_account() -> runner_module.AccountState:
    return runner_module.AccountState(
        deposit=10_000_000, total_equity=10_000_000, day_start_equity=10_000_000
    )


async def test_risk_reject_rolls_back_entry_and_allows_refire() -> None:
    r = _runner()
    strat = r.strategies["005930"]
    bar = _bar()

    [signal] = strat.on_bar(bar)
    assert signal.side is Side.BUY
    assert strat.state["last_entry_ts"] is not None  # 발사 시점에 '보유 중' 기록

    order = await r._evaluate_signal(signal, bar, _broke_account())
    assert order is None
    # 거절됐으므로 진입 기록이 롤백 — 종목 잠김 방지.
    assert strat.state.get("last_entry_ts") is None

    # 다음 봉에서 다시 발사 가능.
    next_bar = _bar(ts=bar.ts + timedelta(minutes=1))
    assert strat.on_bar(next_bar) != []


async def test_dry_run_simulated_buy_keeps_entry_state() -> None:
    """dry-run 시뮬 체결은 진입 성공 — 롤백하면 매 봉 재발사돼 라이브와 갈라진다."""
    r = _runner(dry_run=True)
    strat = r.strategies["005930"]
    bar = _bar()

    [signal] = strat.on_bar(bar)
    order = await r._evaluate_signal(signal, bar, _rich_account())
    assert order is None  # dry-run 은 송신 없이 None
    assert strat.state["last_entry_ts"] is not None  # 진입 유지

    # '보유 중' 이므로 다음 봉에서 추가 발사 없음.
    next_bar = _bar(ts=bar.ts + timedelta(minutes=1))
    assert strat.on_bar(next_bar) == []


async def test_place_order_failure_rolls_back_entry() -> None:
    r = _runner()
    r.notifier = AsyncMock()
    strat = r.strategies["005930"]
    bar = _bar()

    [_signal] = strat.on_bar(bar)
    assert strat.state["last_entry_ts"] is not None
    order = Order(symbol="005930", side=Side.BUY, quantity=1, order_type=OrderType.MARKET)

    with patch.object(
        runner_module,
        "place_order",
        AsyncMock(side_effect=KiwoomError(code="500", message="server error")),
    ):
        await r._place_and_track(rest=object(), order=order, bar=bar, account=_rich_account())  # type: ignore[arg-type]

    assert strat.state.get("last_entry_ts") is None


async def test_rollback_noop_when_already_exited() -> None:
    """청산 후 상태(last_exit > last_entry)의 entry 는 이번 시그널 기록이 아님 — 보존."""
    r = _runner()
    strat = r.strategies["005930"]
    t0 = datetime.now(KST)
    strat.state["last_entry_ts"] = t0
    strat.state["last_exit_ts"] = t0 + timedelta(minutes=10)

    r._rollback_entry_state("005930")

    assert strat.state["last_entry_ts"] == t0
