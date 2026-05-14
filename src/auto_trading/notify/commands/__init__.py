"""텔레그램 슬래시 명령 핸들러 — 책임별 모듈 분리.

`telegram_commands.TelegramCommandHandler` 의 long-poll 본체에서 명령별 분기를
이 패키지로 분리:
- `status.py` — read-only 상태 조회 (`/status` `/balance` `/positions` `/pnl` `/last_signal`)
- `control.py` — 러너 제어 (`/pause` `/resume` `/stop` `/unpause`)
- `insight.py` — `InsightCooldown` (LLM 호출 + 비용 가드 cooldown)
- `_dispatch.py` — `CommandContext` + `dispatch_sync` (동기 명령 dispatch table)

`/help`·미등록 명령은 `state.HELP_TEXT` 를 그대로 반환 — 별도 핸들러 파일 X.
"""

from ._dispatch import (
    BalanceRefreshAction,
    CommandContext,
    ControlAction,
    StateProvider,
    UnpauseAction,
    dispatch_async,
    dispatch_sync,
)
from .insight import InsightAction, InsightCooldown

__all__ = [
    "BalanceRefreshAction",
    "CommandContext",
    "ControlAction",
    "InsightAction",
    "InsightCooldown",
    "StateProvider",
    "UnpauseAction",
    "dispatch_async",
    "dispatch_sync",
]
