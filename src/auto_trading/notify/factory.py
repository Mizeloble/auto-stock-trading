"""Notifier 팩토리. 설정에 따라 적절한 백엔드를 골라 반환한다."""

from __future__ import annotations

from ..config import Settings, get_settings
from .base import NoopNotifier, Notifier
from .telegram import TelegramNotifier


def build_notifier(settings: Settings | None = None) -> Notifier:
    s = settings or get_settings()
    if s.has_telegram:
        return TelegramNotifier(
            token=s.telegram_bot_token.get_secret_value(),
            chat_id=s.telegram_chat_id,
        )
    return NoopNotifier()
