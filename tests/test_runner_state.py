"""PaperRunner.state_snapshot() + 시그널 이력 + 텔레그램 v3 제어 상태 회귀."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from auto_trading._common import Bar
from auto_trading.account.balance import AccountSnapshot, Holding
from auto_trading.account.orders import OrderResult
from auto_trading.risk.manager import AccountState
from auto_trading.runner import PaperRunner, RunnerControlState
from auto_trading.runner import paper as runner_module
from auto_trading.strategy.base import Side, Signal
from auto_trading.strategy.sma_crossover import SmaCrossover


def _snapshot() -> AccountSnapshot:
    return AccountSnapshot(
        deposit=Decimal(1_000_000),
        total_evaluation=Decimal(1_500_000),
        total_pnl=Decimal(50_000),
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


def _runner() -> PaperRunner:
    return PaperRunner(strategy=SmaCrossover(), symbols=["005930"])


async def test_state_snapshot_initial_state() -> None:
    """run() 호출 전엔 _started_at / _snapshot / _account 모두 None."""
    r = _runner()
    state = r.state_snapshot()

    assert state.started_at is None
    assert state.snapshot is None
    assert state.account is None
    assert state.recent_signals == {}
    assert state.symbols == ["005930"]
    assert state.strategies == {"005930": "SmaCrossover"}
    assert state.mode == "mock"
    assert state.dry_run is False
    assert state.balance_failures == 0


async def test_state_snapshot_after_balance_fetch_exposes_raw_and_derived() -> None:
    """_snapshot_account() 가 raw AccountSnapshot 와 derived AccountState 둘 다 보존."""
    r = _runner()

    with patch.object(runner_module, "get_balance", return_value=_snapshot()):
        await r._snapshot_account(rest=object(), previous=None)  # type: ignore[arg-type]

    state = r.state_snapshot()
    assert state.snapshot is not None
    assert state.snapshot.deposit == Decimal(1_000_000)
    assert state.snapshot.holdings[0].symbol == "005930"
    assert state.account is not None
    assert state.account.positions == {"005930": 10}


async def test_state_snapshot_returns_shallow_copies() -> None:
    """반환된 dict/list 가 러너 내부와 같은 객체를 공유하면 race 위험 — 복사 확인."""
    r = _runner()
    r._recent_signals["005930"] = Signal(symbol="005930", side=Side.BUY, weight=1.0)

    state = r.state_snapshot()
    state.recent_signals["999999"] = Signal(symbol="999999", side=Side.SELL, weight=1.0)
    state.symbols.append("000660")

    # 내부 상태에 영향 주면 안 됨
    assert "999999" not in r._recent_signals
    assert "000660" not in r.symbols


async def test_on_bar_records_signal_in_history_even_when_rejected() -> None:
    """리스크 매니저가 거절해도 시그널 자체는 _recent_signals 에 남아야 한다."""
    r = PaperRunner(strategy=SmaCrossover(), symbols=["005930"], dry_run=True)
    # 매도 시그널인데 보유 0 → 리스크 매니저 거절 시나리오
    r.strategy.on_bar = lambda _b: [  # type: ignore[method-assign]
        Signal(symbol="005930", side=Side.SELL, weight=1.0, reason="forced")
    ]
    bar = Bar(
        symbol="005930",
        ts=datetime(2026, 4, 25, 10, 0),
        open=70_000,
        high=70_000,
        low=70_000,
        close=70_000,
        volume=1,
    )
    account = AccountState(
        deposit=10_000_000,
        total_equity=10_000_000,
        positions={},
        avg_prices={},
        day_start_equity=10_000_000,
    )

    await r._on_bar(rest=object(), bar=bar, account=account)  # type: ignore[arg-type]

    assert "005930" in r._recent_signals
    assert r._recent_signals["005930"].reason == "forced"
    assert r._recent_signals["005930"].side is Side.SELL


async def test_pause_skips_order_but_records_signal() -> None:
    """/pause → control_state=PAUSED → place_order 호출 X, 시그널은 _recent_signals 에 기록."""
    r = PaperRunner(strategy=SmaCrossover(), symbols=["005930"], dry_run=False)
    r.strategy.on_bar = lambda _b: [  # type: ignore[method-assign]
        Signal(symbol="005930", side=Side.BUY, weight=1.0, reason="paused-test")
    ]
    r.pause()
    assert r._control_state is RunnerControlState.PAUSED

    bar = Bar(
        symbol="005930",
        ts=datetime(2026, 4, 28, 10, 0),
        open=70_000,
        high=70_000,
        low=70_000,
        close=70_000,
        volume=1,
    )
    account = AccountState(
        deposit=10_000_000,
        total_equity=10_000_000,
        positions={},
        avg_prices={},
        day_start_equity=10_000_000,
    )

    with patch.object(runner_module, "place_order", new=AsyncMock()) as mock_place:
        await r._on_bar(rest=object(), bar=bar, account=account)  # type: ignore[arg-type]

    # PAUSED 면 실제 주문 송신은 0회
    assert mock_place.call_count == 0
    # 시그널은 그대로 기록 — /last_signal 응답 정확도 유지
    assert "005930" in r._recent_signals
    assert r._recent_signals["005930"].reason == "paused-test"


async def test_running_state_actually_places_order_when_paused_path_skipped() -> None:
    """PAUSED 비교군 — RUNNING 일 때는 같은 시그널이 place_order 까지 도달."""
    r = PaperRunner(strategy=SmaCrossover(), symbols=["005930"], dry_run=False)
    r.strategy.on_bar = lambda _b: [  # type: ignore[method-assign]
        Signal(symbol="005930", side=Side.BUY, weight=1.0, reason="control-test")
    ]
    assert r._control_state is RunnerControlState.RUNNING

    bar = Bar(
        symbol="005930",
        ts=datetime(2026, 4, 28, 10, 0),
        open=70_000,
        high=70_000,
        low=70_000,
        close=70_000,
        volume=1,
    )
    account = AccountState(
        deposit=10_000_000,
        total_equity=10_000_000,
        positions={},
        avg_prices={},
        day_start_equity=10_000_000,
    )

    with patch.object(
        runner_module,
        "place_order",
        new=AsyncMock(return_value=OrderResult(order_no="X1", raw={})),
    ) as mock_place:
        await r._on_bar(rest=object(), bar=bar, account=account)  # type: ignore[arg-type]

    assert mock_place.call_count == 1


async def test_resume_returns_to_running_from_paused() -> None:
    r = PaperRunner(strategy=SmaCrossover(), symbols=["005930"])
    r.pause()
    assert r._control_state is RunnerControlState.PAUSED
    r.resume()
    assert r._control_state is RunnerControlState.RUNNING


async def test_request_stop_is_terminal_pause_resume_noop() -> None:
    """STOPPING 진입 후 pause/resume 은 무시 — 종료 진행을 되돌릴 수 없게."""
    r = PaperRunner(strategy=SmaCrossover(), symbols=["005930"])
    r.request_stop()
    assert r._control_state is RunnerControlState.STOPPING

    r.pause()
    assert r._control_state is RunnerControlState.STOPPING  # 변하지 않음
    r.resume()
    assert r._control_state is RunnerControlState.STOPPING


async def test_state_snapshot_exposes_control_state_value() -> None:
    """텔레그램 /status 가 읽는 control_state 값이 enum.value 와 일치."""
    r = PaperRunner(strategy=SmaCrossover(), symbols=["005930"])
    assert r.state_snapshot().control_state == "running"
    r.pause()
    assert r.state_snapshot().control_state == "paused"
    r.request_stop()
    assert r.state_snapshot().control_state == "stopping"


async def test_on_bar_overwrites_prior_signal_per_symbol() -> None:
    """같은 종목에 새 시그널이 뜨면 이전 건은 덮어써진다."""
    r = PaperRunner(strategy=SmaCrossover(), symbols=["005930"], dry_run=True)
    bar = Bar(
        symbol="005930",
        ts=datetime(2026, 4, 25, 10, 0),
        open=70_000,
        high=70_000,
        low=70_000,
        close=70_000,
        volume=1,
    )
    account = AccountState(
        deposit=10_000_000,
        total_equity=10_000_000,
        day_start_equity=10_000_000,
    )

    r.strategy.on_bar = lambda _b: [  # type: ignore[method-assign]
        Signal(symbol="005930", side=Side.BUY, weight=1.0, reason="first")
    ]
    await r._on_bar(rest=object(), bar=bar, account=account)  # type: ignore[arg-type]

    r.strategy.on_bar = lambda _b: [  # type: ignore[method-assign]
        Signal(symbol="005930", side=Side.SELL, weight=1.0, reason="second")
    ]
    await r._on_bar(rest=object(), bar=bar, account=account)  # type: ignore[arg-type]

    assert len(r._recent_signals) == 1
    assert r._recent_signals["005930"].reason == "second"
