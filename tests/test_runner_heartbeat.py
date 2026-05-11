"""runner 의 heartbeat background task 회귀.

29단계 PR3 (#13).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import time as dt_time
from pathlib import Path
from unittest.mock import AsyncMock

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


# 29단계 회귀(2026-05-11) — 정상 종료 sentinel 라이프사이클.


async def test_run_clears_stopped_sentinel_on_startup(tmp_path: Path) -> None:
    """run() 시작 시 이전 사이클의 stopped sentinel 을 정리해야 cron 이 다음
    누락 상황에서 알람을 다시 보낼 수 있다."""
    hb_path = tmp_path / "runner.txt"
    stopped = tmp_path / "stopped.txt"
    stopped.write_text("from_yesterday\n", encoding="utf-8")  # 어제 종료 흔적

    r = PaperRunner(
        strategy=SmaCrossover(),
        symbols=[],  # day-trade — universe 미정 허용
        liquidate_at=dt_time(15, 15),
        heartbeat=Heartbeat(path=hb_path, stopped_path=stopped, interval_sec=300),
        notifier=AsyncMock(),
    )
    # day-trade universe 대기 중 외부 stop — _wait_universe_or_stop() 가 False 반환하는
    # 빠른 경로. run() 을 task 로 띄운 뒤 곧장 request_stop 으로 깨워야 시작 시 sentinel
    # 정리 단계가 실행된다.
    task = asyncio.create_task(r.run())
    await asyncio.sleep(0.05)
    r.request_stop()
    await asyncio.wait_for(task, timeout=2.0)

    # 시작 시 clear_stopped 가 sentinel 을 지웠다가, 빠른 경로 종료에서 mark_stopped 로 다시 touch.
    assert stopped.exists(), "정상 종료라면 sentinel 이 재기록되어야 함"
    content = stopped.read_text(encoding="utf-8")
    assert content.strip() != "from_yesterday"


async def test_run_marks_stopped_sentinel_on_stopped_before_universe(tmp_path: Path) -> None:
    """universe 미정 상태에서 외부 stop 으로 종료해도 mark_stopped 가 호출돼야 cron 침묵."""
    hb_path = tmp_path / "runner.txt"
    stopped = tmp_path / "stopped.txt"

    r = PaperRunner(
        strategy=SmaCrossover(),
        symbols=[],
        liquidate_at=dt_time(15, 15),
        heartbeat=Heartbeat(path=hb_path, stopped_path=stopped, interval_sec=300),
        notifier=AsyncMock(),
    )
    task = asyncio.create_task(r.run())
    await asyncio.sleep(0.05)
    r.request_stop()
    await asyncio.wait_for(task, timeout=2.0)

    assert stopped.exists()
