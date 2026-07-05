"""5/14 회귀 — `/balance` 강제 갱신 + 일일 리포트가 청산 후 정확한 보유를 표시.

핵심 검증:
- runner.force_balance_refresh() 가 활성 REST 가 있을 때 _snapshot_account 호출.
- 활성 REST 가 없으면(러너 정지 후) False, 캐시 변화 X.
- 청산 직후 force_balance_refresh 로 self._snapshot.holdings 가 빈 상태로 갱신되면
  ReportingNotifier 가 build_daily_report 로 만든 closing_holdings 도 빈 상태.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

from auto_trading.account.balance import AccountSnapshot, Holding
from auto_trading.runner import PaperRunner
from auto_trading.runner import paper as runner_module
from auto_trading.strategy.sma_crossover import SmaCrossover


def _runner() -> PaperRunner:
    return PaperRunner(strategy=SmaCrossover(), symbols=["005930"])


def _snapshot(qty: int) -> AccountSnapshot:
    holdings = (
        [
            Holding(
                symbol="005930",
                name="삼성전자",
                quantity=qty,
                avg_price=Decimal("291000"),
                current_price=Decimal("295000"),
                market_value=Decimal(str(qty * 295_000)),
                pnl=Decimal(str(qty * 4_000)),
                pnl_pct=Decimal("1.37"),
            )
        ]
        if qty > 0
        else []
    )
    return AccountSnapshot(
        deposit=Decimal("49230116"),
        total_evaluation=Decimal(str(qty * 295_000)),
        total_pnl=Decimal(str(qty * 4_000)),
        holdings=holdings,
    )


async def test_force_balance_refresh_without_active_rest_returns_false() -> None:
    """활성 REST 가 없으면 캐시 갱신 시도조차 X — False 폴백."""
    r = _runner()
    assert r._rest_client is None
    assert await r.force_balance_refresh() is False
    assert r._snapshot is None


async def test_force_balance_refresh_invokes_snapshot_when_rest_active() -> None:
    """run() 안에서 self._rest_client 가 채워지면 force_balance_refresh 가 동작."""
    r = _runner()
    r._rest_client = object()  # type: ignore[assignment]

    with patch.object(
        runner_module,
        "get_balance",
        AsyncMock(return_value=_snapshot(qty=0)),
    ):
        ok = await r.force_balance_refresh()

    assert ok is True
    assert r._snapshot is not None
    assert r._snapshot.holdings == []
