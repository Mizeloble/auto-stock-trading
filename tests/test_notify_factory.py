"""build_notifier 팩토리 — Settings 의 텔레그램 키 유무에 따라 적절한 백엔드 반환."""

from __future__ import annotations

from pydantic import SecretStr

from auto_trading.config import KiwoomMode, Settings
from auto_trading.notify.base import NoopNotifier
from auto_trading.notify.factory import build_notifier
from auto_trading.notify.telegram import TelegramNotifier


def test_factory_returns_noop_when_telegram_unset() -> None:
    s = Settings(
        kiwoom_mode=KiwoomMode.MOCK,
        kiwoom_app_key=SecretStr("k"),
        kiwoom_app_secret=SecretStr("s"),
        # telegram_* 미설정
    )
    n = build_notifier(s)
    assert isinstance(n, NoopNotifier)


def test_factory_returns_telegram_when_keys_present() -> None:
    s = Settings(
        kiwoom_mode=KiwoomMode.MOCK,
        kiwoom_app_key=SecretStr("k"),
        kiwoom_app_secret=SecretStr("s"),
        telegram_bot_token=SecretStr("12345:ABCDEF"),
        telegram_chat_id="42",
    )
    n = build_notifier(s)
    assert isinstance(n, TelegramNotifier)
