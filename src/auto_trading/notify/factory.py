"""Notifier 팩토리. 설정에 따라 적절한 백엔드를 골라 반환한다."""

from __future__ import annotations

from ..config import Settings, get_settings
from .base import NoopNotifier, Notifier
from .telegram import TelegramNotifier
from .telegram_commands import (
    ControlAction,
    InsightAction,
    StateProvider,
    TelegramCommandHandler,
    UnpauseAction,
)


def build_notifier(settings: Settings | None = None) -> Notifier:
    s = settings or get_settings()
    if s.has_telegram:
        return TelegramNotifier(
            token=s.telegram_bot_token.get_secret_value(),
            chat_id=s.telegram_chat_id,
        )
    return NoopNotifier()


def build_command_handler(
    state_provider: StateProvider,
    settings: Settings | None = None,
    *,
    on_pause: ControlAction | None = None,
    on_resume: ControlAction | None = None,
    on_stop: ControlAction | None = None,
    on_unpause: UnpauseAction | None = None,
    on_insight: InsightAction | None = None,
) -> TelegramCommandHandler | None:
    """텔레그램 인커밍 명령 핸들러. 화이트리스트 미설정이면 None 리턴.

    on_pause/on_resume/on_stop 을 안 넘기면 텔레그램 v3 명령은 "비활성" 응답 — 기존 v2
    호환성 유지. CLI 에서는 runner.pause / runner.resume / runner.request_stop 을 그대로
    바인딩해 넘긴다.

    on_unpause 는 29단계 PR11 — 연속 손절 자동 일시중지의 수동 해제.
    `runner.unpause_loss_tracker` 를 그대로 바인딩한다 (bool 반환).

    on_insight 는 22단계 Phase B — Anthropic 키 + 일일 리포트 데이터가 있을 때만 주입.
    """
    s = settings or get_settings()
    if not s.has_telegram_commands:
        return None
    return TelegramCommandHandler(
        token=s.telegram_bot_token.get_secret_value(),
        allowed_chat_ids=s.parsed_allowed_chat_ids,
        state_provider=state_provider,
        on_pause=on_pause,
        on_resume=on_resume,
        on_stop=on_stop,
        on_unpause=on_unpause,
        on_insight=on_insight,
    )
