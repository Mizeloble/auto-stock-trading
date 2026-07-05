"""PaperRunner._refresh_correlations_if_needed — 29단계 PR28 (#15 Phase 2).

캐시 동작 (날짜·보유 변경 시만 재계산), 한도 비활성 시 스킵, fetch 실패 시 보존.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

from auto_trading.risk.manager import AccountState, RiskLimits, RiskManager
from auto_trading.runner import PaperRunner
from auto_trading.strategy.sma_crossover import SmaCrossover


def _runner(*, max_correlation: float = 0.0) -> PaperRunner:
    return PaperRunner(
        strategy=SmaCrossover(),
        symbols=["005930"],
        risk=RiskManager(
            limits=RiskLimits(
                daily_loss_limit=0.03,
                max_position_correlation=max_correlation,
            )
        ),
    )


def _account(positions: dict[str, int] | None = None) -> AccountState:
    return AccountState(
        deposit=1_000_000,
        total_equity=1_000_000,
        positions=positions or {},
        avg_prices={},
        day_start_equity=1_000_000,
    )


async def test_refresh_skipped_when_correlation_limit_disabled() -> None:
    """max_position_correlation=0 (기본) → fetch 호출 0."""
    r = _runner(max_correlation=0.0)
    fetch = AsyncMock(return_value={})
    with patch("auto_trading.runner.paper.refresh_correlations", fetch):
        await r._refresh_correlations_if_needed(rest=object(), account=_account({"AAA": 5}))  # type: ignore[arg-type]
    fetch.assert_not_called()


async def test_refresh_skipped_when_no_holdings() -> None:
    """보유 0 → 가드 무의미 → fetch 호출 0 + 빈 dict."""
    r = _runner(max_correlation=0.7)
    fetch = AsyncMock(return_value={})
    with patch("auto_trading.runner.paper.refresh_correlations", fetch):
        await r._refresh_correlations_if_needed(rest=object(), account=_account({}))  # type: ignore[arg-type]
    fetch.assert_not_called()
    assert r._holding_correlations == {}


async def test_refresh_calls_fetch_first_time() -> None:
    """첫 호출 → fetch 1회 + 캐시 저장."""
    r = _runner(max_correlation=0.7)
    pairs = {("AAA", "BBB"): 0.8}
    fetch = AsyncMock(return_value=pairs)
    with patch("auto_trading.runner.paper.refresh_correlations", fetch):
        await r._refresh_correlations_if_needed(
            rest=object(),  # type: ignore[arg-type]
            account=_account({"AAA": 5, "BBB": 3}),
        )
    fetch.assert_called_once()
    assert r._holding_correlations == pairs
    assert r._correlations_refresh_symbols == frozenset({"AAA", "BBB"})


async def test_refresh_cached_when_same_day_and_holdings() -> None:
    """동일 날짜 + 동일 보유 → 두 번째 호출은 fetch 스킵."""
    r = _runner(max_correlation=0.7)
    fetch = AsyncMock(return_value={("AAA", "BBB"): 0.5})
    with patch("auto_trading.runner.paper.refresh_correlations", fetch):
        await r._refresh_correlations_if_needed(
            rest=object(),  # type: ignore[arg-type]
            account=_account({"AAA": 5, "BBB": 3}),
        )
        await r._refresh_correlations_if_needed(
            rest=object(),  # type: ignore[arg-type]
            account=_account({"AAA": 5, "BBB": 3}),
        )
    assert fetch.call_count == 1


async def test_refresh_recomputes_when_holdings_change() -> None:
    """보유 종목 셋이 바뀌면 같은 날짜라도 재계산."""
    r = _runner(max_correlation=0.7)
    fetch = AsyncMock(return_value={})
    with patch("auto_trading.runner.paper.refresh_correlations", fetch):
        await r._refresh_correlations_if_needed(
            rest=object(),  # type: ignore[arg-type]
            account=_account({"AAA": 5}),
        )
        # 종목 추가
        await r._refresh_correlations_if_needed(
            rest=object(),  # type: ignore[arg-type]
            account=_account({"AAA": 5, "BBB": 3}),
        )
    assert fetch.call_count == 2


async def test_refresh_recomputes_when_date_changes() -> None:
    """날짜 변경 시 (전일 캐시) 재계산."""
    r = _runner(max_correlation=0.7)
    r._correlations_refresh_date = date(2026, 5, 10)  # 어제
    r._correlations_refresh_symbols = frozenset({"AAA"})
    r._holding_correlations = {("AAA", "BBB"): 0.5}  # 더미 캐시
    fetch = AsyncMock(return_value={("AAA", "BBB"): 0.9})
    with patch("auto_trading.runner.paper.refresh_correlations", fetch):
        await r._refresh_correlations_if_needed(
            rest=object(),  # type: ignore[arg-type]
            account=_account({"AAA": 5}),
        )
    fetch.assert_called_once()
    assert r._holding_correlations == {("AAA", "BBB"): 0.9}


async def test_refresh_preserves_cache_on_fetch_failure() -> None:
    """fetch 예외 → warning + 마지막 캐시 보존 (BUY 가드 무방비 회피)."""
    r = _runner(max_correlation=0.7)
    r._correlations_refresh_date = date(2026, 5, 10)
    r._correlations_refresh_symbols = frozenset({"AAA"})
    cached = {("AAA", "BBB"): 0.5}
    r._holding_correlations = dict(cached)
    fetch = AsyncMock(side_effect=RuntimeError("api down"))
    with patch("auto_trading.runner.paper.refresh_correlations", fetch):
        await r._refresh_correlations_if_needed(
            rest=object(),  # type: ignore[arg-type]
            account=_account({"AAA": 5, "BBB": 3}),
        )
    assert r._holding_correlations == cached  # 그대로 보존
    # 캐시 키는 갱신 안 됨 — 다음 사이클에서 다시 시도.
    assert r._correlations_refresh_symbols == frozenset({"AAA"})


async def test_evaluate_signal_passes_correlations_to_risk() -> None:
    """_evaluate_signal 이 캐시된 페어 dict 를 risk.evaluate 로 주입."""
    from auto_trading._common import Bar
    from auto_trading.strategy.base import Side, Signal

    r = _runner(max_correlation=0.7)
    pairs = {("AAA", "TGT"): 0.85}
    r._holding_correlations = dict(pairs)
    bar = Bar(symbol="TGT", ts=None, open=0, high=0, low=0, close=70_000, volume=0)  # type: ignore[arg-type]
    account = _account({"AAA": 5})
    signal = Signal(symbol="TGT", side=Side.BUY, weight=1.0)

    spy = patch.object(r.risk, "evaluate", wraps=r.risk.evaluate)
    with spy as wrapper:
        await r._evaluate_signal(signal, bar, account)
    # holding_correlations 가 kwarg 로 넘어갔는지 확인.
    _, kwargs = wrapper.call_args
    assert kwargs["holding_correlations"] == pairs
