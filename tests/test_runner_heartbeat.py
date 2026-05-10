"""runner 의 heartbeat background task 회귀.

29단계 PR3 (#13).
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from auto_trading.heartbeat import Heartbeat
from auto_trading.runner import PaperRunner, RunnerControlState
from auto_trading.strategy.sma_crossover import SmaCrossover


def _runner_with_heartbeat(path: Path, interval_sec: int = 1800) -> PaperRunner:
    return PaperRunner(
        strategy=SmaCrossover(),
        symbols=["005930"],
        heartbeat=Heartbeat(path=path, interval_sec=interval_sec),
    )


async def test_heartbeat_loop_writes_immediately_on_start(tmp_path: Path) -> None:
    """시동 시 1회 write — cron 이 즉시 봇 살아있음 인지."""
    r = _runner_with_heartbeat(tmp_path / "runner.txt")

    # interval 매우 길게 잡고, 시작 직후 file 존재만 확인.
    task = asyncio.create_task(r._heartbeat_loop())
    await asyncio.sleep(0.05)
    assert (tmp_path / "runner.txt").exists()

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_heartbeat_loop_stops_on_stopping_state(tmp_path: Path) -> None:
    """STOPPING 진입 시 다음 sleep wake 에서 loop 종료."""
    r = _runner_with_heartbeat(tmp_path / "runner.txt", interval_sec=1)

    task = asyncio.create_task(r._heartbeat_loop())
    await asyncio.sleep(0.05)  # 즉시 첫 write 끝
    r._control_state = RunnerControlState.STOPPING

    # interval 1초 후 다음 사이클에서 종료.
    await asyncio.wait_for(task, timeout=2.0)
    assert task.done()


async def test_heartbeat_loop_noop_when_unset() -> None:
    r = PaperRunner(strategy=SmaCrossover(), symbols=["005930"])
    # heartbeat None 이면 즉시 return.
    await r._heartbeat_loop()


async def test_heartbeat_loop_cancels_cleanly(tmp_path: Path) -> None:
    """cancel 시 CancelledError 를 catch 해서 깨끗이 종료."""
    r = _runner_with_heartbeat(tmp_path / "runner.txt", interval_sec=300)
    task = asyncio.create_task(r._heartbeat_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    # CancelledError 가 task 안에서 잡혀서 정상 종료 — 호출자에 전파 X.
    await task
    assert task.done()
