"""Notifier 팩토리. 설정에 따라 적절한 백엔드를 골라 반환한다."""

from __future__ import annotations

from ..config import Settings, get_settings
from .base import NoopNotifier, Notifier
from .telegram import TelegramNotifier
from .telegram_commands import StateProvider, TelegramCommandHandler


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
) -> TelegramCommandHandler | None:
    """텔레그램 인커밍 명령 핸들러. 화이트리스트 미설정이면 None 리턴."""
    s = settings or get_settings()
    if not s.has_telegram_commands:
        return None
    return TelegramCommandHandler(
        token=s.telegram_bot_token.get_secret_value(),
        allowed_chat_ids=s.parsed_allowed_chat_ids,
        state_provider=state_provider,
    )
