"""build_notifier 팩토리 — Settings 의 텔레그램 키 유무에 따라 적절한 백엔드 반환."""

from __future__ import annotations

from pydantic import SecretStr

from auto_trading.config import KiwoomMode, Settings
from auto_trading.notify.base import NoopNotifier
from auto_trading.notify.discord import DiscordNotifier
from auto_trading.notify.factory import build_notifier
from auto_trading.notify.multi import MultiNotifier
from auto_trading.notify.telegram import TelegramNotifier


def test_factory_returns_noop_when_telegram_unset() -> None:
    # _env_file=None 으로 사용자 로컬 .env 의존 차단 (tests/CLAUDE.md 규칙).
    s = Settings(
        _env_file=None,  # type: ignore[call-arg]
        kiwoom_mode=KiwoomMode.MOCK,
        kiwoom_app_key=SecretStr("k"),
        kiwoom_app_secret=SecretStr("s"),
        telegram_bot_token=SecretStr(""),
        telegram_chat_id="",
    )
    n = build_notifier(s)
    assert isinstance(n, NoopNotifier)


def test_factory_returns_telegram_when_keys_present() -> None:
    s = Settings(
        _env_file=None,  # type: ignore[call-arg]
        kiwoom_mode=KiwoomMode.MOCK,
        kiwoom_app_key=SecretStr("k"),
        kiwoom_app_secret=SecretStr("s"),
        telegram_bot_token=SecretStr("12345:ABCDEF"),
        telegram_chat_id="42",
    )
    n = build_notifier(s)
    assert isinstance(n, TelegramNotifier)


# ---- 29단계 PR25 (#12) — Discord + MultiNotifier --------------------------


def test_factory_returns_discord_when_only_webhook_set() -> None:
    s = Settings(
        _env_file=None,  # type: ignore[call-arg]
        kiwoom_mode=KiwoomMode.MOCK,
        kiwoom_app_key=SecretStr("k"),
        kiwoom_app_secret=SecretStr("s"),
        telegram_bot_token=SecretStr(""),
        telegram_chat_id="",
        discord_webhook_url=SecretStr("https://discord.com/api/webhooks/x/y"),
    )
    n = build_notifier(s)
    assert isinstance(n, DiscordNotifier)


def test_factory_returns_multi_when_both_active() -> None:
    s = Settings(
        _env_file=None,  # type: ignore[call-arg]
        kiwoom_mode=KiwoomMode.MOCK,
        kiwoom_app_key=SecretStr("k"),
        kiwoom_app_secret=SecretStr("s"),
        telegram_bot_token=SecretStr("12345:ABCDEF"),
        telegram_chat_id="42",
        discord_webhook_url=SecretStr("https://discord.com/api/webhooks/x/y"),
    )
    n = build_notifier(s)
    assert isinstance(n, MultiNotifier)
