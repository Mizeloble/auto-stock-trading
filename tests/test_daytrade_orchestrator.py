"""daytrade.orchestrator.discover_and_arm 회귀 테스트.

검증:
- 후보 0건이면 set_universe 호출 X, blocked·chosen 모두 비어 있고 안내 문구.
- 후보 N건이지만 게이트 모두 차단이면 set_universe 호출 X.
- 일부 통과/일부 차단 — 통과 종목만 set_universe 에 들어가고 blocked 에 차단 사유 기록.
- 균등 분할 weight = 1/top_n 이 IntradayMomentum 에 주입.
- 같은 시장 후보가 여럿이어도 index_gate 는 시장당 한 번만 호출 (캐싱).
"""

from __future__ import annotations

from datetime import time
from unittest.mock import AsyncMock

import pytest

from auto_trading._common import KST
from auto_trading.daytrade import orchestrator as orch
from auto_trading.market.index import IndexGate
from auto_trading.market.rank import RankedSymbol
from auto_trading.runner import PaperRunner
from auto_trading.strategy.intraday import IntradayMomentum


def _runner() -> PaperRunner:
    return PaperRunner(liquidate_at=time(15, 15, tzinfo=KST))


def _gate(market: str, *, above: bool) -> IndexGate:
    return IndexGate(
        market=market,  # type: ignore[arg-type]
        last=100.0,
        sma5=90.0 if above else 110.0,
        sma20=80.0 if above else 120.0,
        above_5_and_20=above,
        bars=30,
    )


async def test_discover_no_candidates_returns_empty_and_no_universe(monkeypatch) -> None:
    runner = _runner()
    monkeypatch.setattr(orch, "top_volume", AsyncMock(return_value=[]))
    gate_mock = AsyncMock()
    monkeypatch.setattr(orch, "index_gate", gate_mock)

    result = await orch.discover_and_arm(rest=object(), runner=runner, top_n=3)  # type: ignore[arg-type]

    assert result.chosen == []
    assert result.blocked == []
    assert "후보를 가져오지 못했습니다" in result.note_ko
    gate_mock.assert_not_called()
    assert runner.symbols == []


async def test_discover_all_blocked_does_not_set_universe(monkeypatch) -> None:
    runner = _runner()
    candidates = [
        RankedSymbol(symbol="005930", market="KOSPI", name="삼성전자", volume=10, price=70000),
        RankedSymbol(symbol="247540", market="KOSDAQ", name="에코프로비엠", volume=8, price=200000),
    ]
    monkeypatch.setattr(orch, "top_volume", AsyncMock(return_value=candidates))
    monkeypatch.setattr(
        orch,
        "index_gate",
        AsyncMock(side_effect=lambda _r, m: _gate(m, above=False)),
    )

    result = await orch.discover_and_arm(rest=object(), runner=runner, top_n=3)  # type: ignore[arg-type]

    assert result.chosen == []
    assert sorted(s for s, _ in result.blocked) == ["005930", "247540"]
    assert all("below_sma" in r for _, r in result.blocked)
    assert runner.symbols == []
    assert "차단" in result.note_ko or "대기" in result.note_ko
    # chosen 이 비어도 차단 후보 이름이 result.names 에 담겨야 텔레그램이 코드+이름 표시.
    assert result.names == {"005930": "삼성전자", "247540": "에코프로비엠"}


async def test_discover_partial_blocked_sets_universe_for_passing_only(monkeypatch) -> None:
    runner = _runner()
    candidates = [
        RankedSymbol(symbol="005930", market="KOSPI", name="삼성전자", volume=10, price=70000),
        RankedSymbol(symbol="247540", market="KOSDAQ", name="에코프로비엠", volume=8, price=200000),
        RankedSymbol(symbol="000660", market="KOSPI", name="SK하이닉스", volume=5, price=150000),
    ]
    monkeypatch.setattr(orch, "top_volume", AsyncMock(return_value=candidates))
    # KOSPI 통과, KOSDAQ 차단
    monkeypatch.setattr(
        orch,
        "index_gate",
        AsyncMock(side_effect=lambda _r, m: _gate(m, above=(m == "KOSPI"))),
    )

    result = await orch.discover_and_arm(rest=object(), runner=runner, top_n=3)  # type: ignore[arg-type]

    chosen_symbols = [c.symbol for c in result.chosen]
    assert chosen_symbols == ["005930", "000660"]
    assert ("247540", "KOSDAQ_below_sma") in result.blocked
    assert sorted(runner.symbols) == ["000660", "005930"]
    assert all(isinstance(s, IntradayMomentum) for s in runner.strategies.values())


async def test_discover_uses_equal_weight_when_no_factory(monkeypatch) -> None:
    runner = _runner()
    candidates = [
        RankedSymbol(symbol=f"00000{i}", market="KOSPI", name=f"종목{i}", volume=10 - i, price=100)
        for i in range(3)
    ]
    monkeypatch.setattr(orch, "top_volume", AsyncMock(return_value=candidates))
    monkeypatch.setattr(orch, "index_gate", AsyncMock(return_value=_gate("KOSPI", above=True)))

    await orch.discover_and_arm(rest=object(), runner=runner, top_n=3)  # type: ignore[arg-type]

    # weight = 1/3 ≈ 0.333…
    for strat in runner.strategies.values():
        assert isinstance(strat, IntradayMomentum)
        assert strat.params.get("weight") == pytest.approx(1.0 / 3)


async def test_discover_calls_index_gate_once_per_market(monkeypatch) -> None:
    """같은 시장 후보가 여럿이어도 index_gate 는 시장당 1번만."""
    runner = _runner()
    candidates = [
        RankedSymbol(symbol="A1", market="KOSPI", name="a1", volume=10, price=100),
        RankedSymbol(symbol="A2", market="KOSPI", name="a2", volume=9, price=100),
        RankedSymbol(symbol="A3", market="KOSPI", name="a3", volume=8, price=100),
    ]
    monkeypatch.setattr(orch, "top_volume", AsyncMock(return_value=candidates))
    gate_mock = AsyncMock(return_value=_gate("KOSPI", above=True))
    monkeypatch.setattr(orch, "index_gate", gate_mock)

    await orch.discover_and_arm(rest=object(), runner=runner, top_n=3)  # type: ignore[arg-type]

    assert gate_mock.await_count == 1


async def test_discover_custom_factory_used(monkeypatch) -> None:
    runner = _runner()
    candidates = [RankedSymbol(symbol="005930", market="KOSPI", name="삼성", volume=10, price=100)]
    monkeypatch.setattr(orch, "top_volume", AsyncMock(return_value=candidates))
    monkeypatch.setattr(orch, "index_gate", AsyncMock(return_value=_gate("KOSPI", above=True)))

    sentinel = IntradayMomentum(weight=0.42)

    def _factory(_sym, *, market=None):  # type: ignore[no-untyped-def]
        return sentinel

    await orch.discover_and_arm(
        rest=object(),  # type: ignore[arg-type]
        runner=runner,
        top_n=1,
        strategy_factory=_factory,
    )

    assert runner.strategies["005930"] is sentinel


# ---- 23단계 Phase F · 모멘텀 갱신 task --------------------------------------


async def test_start_momentum_loop_updates_tracker_until_cancelled(monkeypatch) -> None:
    """start_momentum_loop 은 update_index_momentum 을 주기적으로 호출한다."""
    import asyncio
    import contextlib

    from auto_trading.market.index import IndexMomentumTracker

    tracker = IndexMomentumTracker()
    update_mock = AsyncMock()
    monkeypatch.setattr(orch, "update_index_momentum", update_mock)

    task = orch.start_momentum_loop(
        rest=object(),  # type: ignore[arg-type]
        tracker=tracker,
        markets=("KOSPI",),
        interval_sec=0,  # 즉시 다음 사이클로
        lookback_min=5,
    )
    # 한 두 사이클 돌게 두고 cancel
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert update_mock.await_count >= 1
    # 첫 호출이 KOSPI + lookback_min=5 로 들어갔는지
    call = update_mock.await_args_list[0]
    assert call.args[2] == "KOSPI"
    assert call.kwargs["lookback_min"] == 5


async def test_momentum_loop_swallows_update_failures(monkeypatch) -> None:
    """update 실패해도 loop 가 죽지 않는다 — 다음 사이클에서 다시 시도."""
    import asyncio
    import contextlib

    from auto_trading.market.index import IndexMomentumTracker

    tracker = IndexMomentumTracker()
    update_mock = AsyncMock(side_effect=RuntimeError("kiwoom 5xx"))
    monkeypatch.setattr(orch, "update_index_momentum", update_mock)

    task = orch.start_momentum_loop(
        rest=object(),  # type: ignore[arg-type]
        tracker=tracker,
        markets=("KOSPI",),
        interval_sec=0,
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    # 실패해도 여러 번 호출됨 (loop 가 swallow + 계속).
    assert update_mock.await_count >= 1


# ---- Phase 1: breadth_gate (시총 200 스냅샷 기반 매수 게이트) ----

from auto_trading.daytrade.profile import DaytradeBreadthGate  # noqa: E402
from auto_trading.market import breadth as bd_mod  # noqa: E402


async def test_breadth_gate_disabled_skips_check(monkeypatch) -> None:
    """enabled=False 면 compute_breadth 호출조차 X — 회귀 0 보장."""
    runner = _runner()
    candidates = [
        RankedSymbol(symbol="005930", market="KOSPI", name="삼성전자", volume=10, price=70000),
    ]
    monkeypatch.setattr(orch, "top_volume", AsyncMock(return_value=candidates))
    monkeypatch.setattr(
        orch, "index_gate", AsyncMock(side_effect=lambda _r, m: _gate(m, above=True))
    )
    compute_mock = AsyncMock()  # 호출되면 안 됨
    monkeypatch.setattr(orch, "compute_breadth", compute_mock)

    config = DaytradeBreadthGate(enabled=False)
    result = await orch.discover_and_arm(
        rest=object(),
        runner=runner,
        top_n=1,
        breadth_gate_config=config,  # type: ignore[arg-type]
    )

    assert [c.symbol for c in result.chosen] == ["005930"]
    compute_mock.assert_not_called()


async def test_breadth_gate_blocks_when_weak(monkeypatch) -> None:
    """enabled=True 이고 강세 미달이면 전 후보 차단 + breadth_blocked reason."""
    runner = _runner()
    candidates = [
        RankedSymbol(symbol="005930", market="KOSPI", name="삼성전자", volume=10, price=70000),
        RankedSymbol(symbol="000660", market="KOSPI", name="SK하이닉스", volume=8, price=150000),
    ]
    monkeypatch.setattr(orch, "top_volume", AsyncMock(return_value=candidates))
    monkeypatch.setattr(
        orch, "index_gate", AsyncMock(side_effect=lambda _r, m: _gate(m, above=True))
    )
    weak = bd_mod.Breadth(
        ts="X",
        sample_size=200,
        advancing_pct=0.2,
        declining_pct=0.7,
        mean_change_pct=-1.0,
    )
    monkeypatch.setattr(orch, "compute_breadth", lambda *_a, **_kw: weak)

    config = DaytradeBreadthGate(enabled=True, min_advancing_pct=0.5, min_mean_change_pct=0.0)
    result = await orch.discover_and_arm(
        rest=object(),
        runner=runner,
        top_n=2,
        breadth_gate_config=config,  # type: ignore[arg-type]
    )

    assert result.chosen == []
    assert runner.symbols == []
    blocked_reasons = {r for _, r in result.blocked}
    assert any("breadth_" in r for r in blocked_reasons)
    assert "breadth 게이트 차단" in result.note_ko


async def test_breadth_gate_passes_when_strong(monkeypatch) -> None:
    """강세면 통과 — 기존 index_gate 흐름 그대로."""
    runner = _runner()
    candidates = [
        RankedSymbol(symbol="005930", market="KOSPI", name="삼성전자", volume=10, price=70000),
    ]
    monkeypatch.setattr(orch, "top_volume", AsyncMock(return_value=candidates))
    monkeypatch.setattr(
        orch, "index_gate", AsyncMock(side_effect=lambda _r, m: _gate(m, above=True))
    )
    strong = bd_mod.Breadth(
        ts="X",
        sample_size=200,
        advancing_pct=0.65,
        declining_pct=0.30,
        mean_change_pct=0.5,
    )
    monkeypatch.setattr(orch, "compute_breadth", lambda *_a, **_kw: strong)

    config = DaytradeBreadthGate(enabled=True, min_advancing_pct=0.5, min_mean_change_pct=0.0)
    result = await orch.discover_and_arm(
        rest=object(),
        runner=runner,
        top_n=1,
        breadth_gate_config=config,  # type: ignore[arg-type]
    )

    assert [c.symbol for c in result.chosen] == ["005930"]


async def test_breadth_gate_fail_open_when_no_data(monkeypatch) -> None:
    """데이터 부족(unavailable) 시 fail-open — 기존 흐름 그대로."""
    runner = _runner()
    candidates = [
        RankedSymbol(symbol="005930", market="KOSPI", name="삼성전자", volume=10, price=70000),
    ]
    monkeypatch.setattr(orch, "top_volume", AsyncMock(return_value=candidates))
    monkeypatch.setattr(
        orch, "index_gate", AsyncMock(side_effect=lambda _r, m: _gate(m, above=True))
    )
    unavail = bd_mod.Breadth.unavailable_instance("no_file")
    monkeypatch.setattr(orch, "compute_breadth", lambda *_a, **_kw: unavail)

    config = DaytradeBreadthGate(enabled=True)
    result = await orch.discover_and_arm(
        rest=object(),
        runner=runner,
        top_n=1,
        breadth_gate_config=config,  # type: ignore[arg-type]
    )

    assert [c.symbol for c in result.chosen] == ["005930"]
