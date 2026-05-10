"""동기 슬래시 명령 dispatch — long-poll 본체에서 분리된 라우팅 단일 진입점.

`/insight` 는 별도 (async + cooldown 상태) — `InsightCooldown.handle()` 가 처리.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..state import HELP_TEXT, RunnerStateSnapshot
from .control import handle_pause, handle_resume, handle_stop, handle_unpause
from .status import (
    handle_balance,
    handle_last_signal,
    handle_pnl,
    handle_positions,
    handle_status,
)

StateProvider = Callable[[], RunnerStateSnapshot]
ControlAction = Callable[[], None]
UnpauseAction = Callable[[], bool]


@dataclass(frozen=True)
class CommandContext:
    """동기 명령 핸들러가 공유하는 의존성 묶음.

    `state_provider` 는 read-only 명령 5종이 호출. control 콜백 4종은 None 이면
    해당 명령이 "비활성" 응답을 돌려준다.
    """

    state_provider: StateProvider
    on_pause: ControlAction | None = None
    on_resume: ControlAction | None = None
    on_stop: ControlAction | None = None
    # 29단계 PR11 (#14 마무리) — 연속 손절 자동 일시중지 수동 해제.
    on_unpause: UnpauseAction | None = None


def dispatch_sync(cmd: str, ctx: CommandContext) -> str:
    """슬래시 명령(`/...`) → 응답 문자열. 미등록 명령은 `/help` 와 동일.

    명령 추가 시 이 함수에만 분기 추가 — 핸들러 모듈은 변경 없음.
    """
    if cmd in ("/start", "/help"):
        return HELP_TEXT
    if cmd == "/pause":
        return handle_pause(ctx.on_pause)
    if cmd == "/resume":
        return handle_resume(ctx.on_resume)
    if cmd == "/stop":
        return handle_stop(ctx.on_stop)
    if cmd == "/unpause":
        return handle_unpause(ctx.on_unpause)
    state = ctx.state_provider()
    if cmd == "/status":
        return handle_status(state)
    if cmd == "/balance":
        return handle_balance(state)
    if cmd == "/positions":
        return handle_positions(state)
    if cmd == "/pnl":
        return handle_pnl(state)
    if cmd == "/last_signal":
        return handle_last_signal(state)
    return HELP_TEXT
