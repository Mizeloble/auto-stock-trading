"""day-trade 모드의 동적 universe (set_universe + universe_ready) 회귀 테스트.

검증:
- liquidate_at 설정 시 빈 symbols/strategies 로 PaperRunner 생성 가능 (swing 은 ValueError 유지)
- set_universe() 가 symbols/strategies 를 갱신하고 _universe_ready 를 set
- set_universe() 검증: 빈 symbols / strategies 누락 → 적절한 예외
- ws 활성 상태에서 set_universe() 재호출 차단
- request_stop() 이 universe 대기 중에도 _universe_ready 를 set 해서 깨움
- swing 모드(liquidate_at=None) 는 종전 동작 100% 유지
"""

from __future__ import annotations

import asyncio
from datetime import time

import pytest

from auto_trading._common import KST
from auto_trading.runner import PaperRunner, RunnerControlState
from auto_trading.strategy.intraday import IntradayMomentum
from auto_trading.strategy.sma_crossover import SmaCrossover


def test_swing_mode_still_requires_strategy_or_strategies() -> None:
    """swing(liquidate_at=None) 에서 strategy/strategies 둘 다 비면 종전대로 ValueError."""
    with pytest.raises(ValueError, match="strategy 또는 strategies"):
        PaperRunner(symbols=["005930"])


def test_day_trade_mode_allows_empty_universe_at_construction() -> None:
    r = PaperRunner(liquidate_at=time(15, 15, tzinfo=KST))
    assert r.symbols == []
    assert r.strategies == {}
    assert r._universe_ready.is_set() is False  # 아직 대기 상태


def test_set_universe_populates_and_signals_ready() -> None:
    r = PaperRunner(liquidate_at=time(15, 15, tzinfo=KST))
    strats = {"005930": IntradayMomentum(weight=0.33), "000660": IntradayMomentum(weight=0.33)}

    r.set_universe(["005930", "000660"], strats)

    assert r.symbols == ["005930", "000660"]
    assert set(r.strategies.keys()) == {"005930", "000660"}
    assert r._universe_ready.is_set() is True


def test_set_universe_rejects_empty_symbols() -> None:
    r = PaperRunner(liquidate_at=time(15, 15, tzinfo=KST))
    with pytest.raises(ValueError, match="symbols 가 비어있습니다"):
        r.set_universe([], {})


def test_set_universe_rejects_missing_strategy_for_symbol() -> None:
    r = PaperRunner(liquidate_at=time(15, 15, tzinfo=KST))
    strats = {"005930": IntradayMomentum()}
    with pytest.raises(ValueError, match="전략이 지정되지 않은 종목"):
        r.set_universe(["005930", "000660"], strats)


def test_set_universe_blocked_after_ws_active() -> None:
    """ws.start() 후에는 set_universe() 호출 차단 (PR-B 범위 — 시작 전 1회)."""
    r = PaperRunner(liquidate_at=time(15, 15, tzinfo=KST))
    r._ws = object()  # type: ignore[assignment] — ws 활성 시뮬

    with pytest.raises(RuntimeError, match="run\\(\\) 진입 전"):
        r.set_universe(["005930"], {"005930": IntradayMomentum()})


async def test_request_stop_wakes_universe_wait() -> None:
    """universe 대기 중에 request_stop() 들어오면 _wait_universe_or_stop() 가 즉시 깨어
    False(stop 분기) 반환. run() 이 안 hang 됨."""
    r = PaperRunner(liquidate_at=time(15, 15, tzinfo=KST))

    async def _trigger_stop() -> None:
        await asyncio.sleep(0.05)
        r.request_stop()

    trigger = asyncio.create_task(_trigger_stop())
    try:
        proceed = await asyncio.wait_for(r._wait_universe_or_stop(), timeout=2.0)
    finally:
        await trigger

    assert proceed is False
    assert r._control_state is RunnerControlState.STOPPING


async def test_set_universe_unblocks_universe_wait_with_proceed_true() -> None:
    """set_universe 호출 시 _wait_universe_or_stop() 가 True 로 깨어남."""
    r = PaperRunner(liquidate_at=time(15, 15, tzinfo=KST))

    async def _set_after_delay() -> None:
        await asyncio.sleep(0.05)
        r.set_universe(["005930"], {"005930": IntradayMomentum()})

    setter = asyncio.create_task(_set_after_delay())
    try:
        proceed = await asyncio.wait_for(r._wait_universe_or_stop(), timeout=2.0)
    finally:
        await setter

    assert proceed is True
    assert r.symbols == ["005930"]


def test_swing_mode_universe_ready_set_at_construction() -> None:
    """기존 swing 모드 (liquidate_at=None + symbols 지정) 는 즉시 ready — 회귀 게이트."""
    r = PaperRunner(strategy=SmaCrossover(), symbols=["005930"])
    assert r._universe_ready.is_set() is True
    assert r.symbols == ["005930"]
