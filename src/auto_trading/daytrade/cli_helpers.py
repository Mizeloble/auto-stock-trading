"""paper-day CLI 의 비-typer 비동기 글루.

cli.py 의 `paper-day` 명령에서만 호출되는 백그라운드 태스크 조합.
- `paper_day_main` / `paper_day_with_commands` — 러너 시동 + (옵션) 텔레그램 명령
  핸들러 동시 폴링 + 발굴 task 생명주기 관리.
- `delayed_discover` — 러너 ws.start 안정화 대기 후 `discover_and_arm` 호출 +
  결과 알림 + universe 0 건 시 명시적 stop 트리거(러너 hang 방지).

여기에 typer 명령 정의는 두지 않는다 — typer 의존은 cli.py 에 격리.
"""

from __future__ import annotations

import asyncio
import contextlib

import structlog
from rich.console import Console

from ..kiwoom.rest import KiwoomRestClient
from ..notify.telegram_commands import TelegramCommandHandler
from ..runner import PaperRunner
from .orchestrator import discover_and_arm

console = Console()


async def paper_day_main(
    runner: PaperRunner, *, top_n: int, discover_delay: int, exclude_etf: bool
) -> None:
    """러너를 백그라운드로 시동 + discover_and_arm 으로 universe 주입."""
    runner_task = asyncio.create_task(runner.run(), name="paper-day-runner")
    discover_task = asyncio.create_task(
        delayed_discover(runner, top_n=top_n, delay=discover_delay, exclude_etf=exclude_etf),
        name="day-discover",
    )
    try:
        await runner_task  # 청산 시각 도달 후 자기 종료
    finally:
        discover_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await discover_task


async def paper_day_with_commands(
    runner: PaperRunner,
    handler: TelegramCommandHandler,
    *,
    top_n: int,
    discover_delay: int,
    exclude_etf: bool,
) -> None:
    """텔레그램 명령 동시 폴링 버전."""
    runner_task = asyncio.create_task(runner.run(), name="paper-day-runner")
    cmd_task = asyncio.create_task(handler.run(), name="tg-commands")
    discover_task = asyncio.create_task(
        delayed_discover(runner, top_n=top_n, delay=discover_delay, exclude_etf=exclude_etf),
        name="day-discover",
    )
    try:
        done, pending = await asyncio.wait(
            {runner_task, cmd_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        for t in pending:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        for t in done:
            t.result()
    finally:
        discover_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await discover_task
        await handler.aclose()


async def delayed_discover(
    runner: PaperRunner, *, top_n: int, delay: int, exclude_etf: bool = True
) -> None:
    """러너 시동 후 `delay` 초 대기 → discover_and_arm.

    실패해도 러너 자체는 청산 시계로 종료될 수 있게 예외를 swallow + 로그.
    """
    log = structlog.get_logger("daytrade.cli")
    if delay > 0:
        await asyncio.sleep(delay)
    try:
        async with KiwoomRestClient() as rest:
            result = await discover_and_arm(rest, runner, top_n=top_n, exclude_etf=exclude_etf)
        console.print(f"[cyan]발굴 결과:[/cyan] {result.note_ko}")
        await runner.notifier.notify(
            "daytrade.discovery_done",
            chosen=[c.symbol for c in result.chosen],
            blocked=result.blocked,
            note=result.note_ko,
        )
        # universe 0 건이면 set_universe() 호출되지 않아 _universe_ready 도 안 set 된다.
        # 청산 시계 task 는 ws.start 이후에 시작되므로 universe 대기 중엔 안 돔 →
        # 러너가 무한 hang. 발굴 실패와 동일하게 명시적으로 stop 트리거.
        # (5/3 21:42 dry-run 에서 게이트 차단으로 chosen=[] → 6시간 hang 실증.)
        if not result.chosen:
            log.info("daytrade.no_candidates_stopping")
            runner.request_stop()
    except Exception as exc:
        log.error("daytrade.discover_failed", error=str(exc), exc_info=exc)
        # 발굴 실패 — universe 안 채워지므로 명시적 stop 으로 hang 회피.
        runner.request_stop()


__all__ = ["delayed_discover", "paper_day_main", "paper_day_with_commands"]
