"""`cli._setup_logging` 회귀.

특히 httpx/httpcore stdlib logger 가 WARNING 으로 강등되는지 — 텔레그램 봇
토큰이 INFO 레벨 풀 URL 로깅으로 노출됐던 2026-05-09 회귀 가드.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Iterator
from unittest.mock import patch

import httpx
import pytest
import respx
import structlog

from auto_trading import cli as cli_module
from auto_trading.notify.telegram import TELEGRAM_API_BASE, TelegramNotifier


@pytest.fixture(autouse=True)
def _restore_structlog() -> Iterator[None]:
    yield
    structlog.reset_defaults()
    logging.getLogger().handlers.clear()


def test_setup_logging_mutes_httpx_below_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    """LOG_LEVEL=INFO 같은 일반 운영에선 httpx 로거가 WARNING."""
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.setenv("KIWOOM_APP_KEY", "k")
    monkeypatch.setenv("KIWOOM_APP_SECRET", "s")
    # Settings cached — 새 환경변수 적용을 위해 cache 비움
    cli_module.get_settings.cache_clear()  # type: ignore[attr-defined]

    cli_module._setup_logging()

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_setup_logging_keeps_httpx_when_root_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    """LOG_LEVEL=DEBUG 면 디버그 의도라 httpx 도 풀림."""
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("KIWOOM_APP_KEY", "k")
    monkeypatch.setenv("KIWOOM_APP_SECRET", "s")
    cli_module.get_settings.cache_clear()  # type: ignore[attr-defined]

    cli_module._setup_logging()

    assert logging.getLogger("httpx").level == logging.DEBUG
    assert logging.getLogger("httpcore").level == logging.DEBUG


async def test_telegram_notify_does_not_leak_token_after_setup(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """_setup_logging 후 sendMessage 호출 시 stdlib log 에 토큰이 안 찍혀야.

    회귀 시나리오: httpx INFO `HTTP Request: POST https://api.telegram.org/bot<TOKEN>/sendMessage`.
    """
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.setenv("KIWOOM_APP_KEY", "k")
    monkeypatch.setenv("KIWOOM_APP_SECRET", "s")
    cli_module.get_settings.cache_clear()  # type: ignore[attr-defined]
    cli_module._setup_logging()

    secret_token = "1234:ABCDEFG_THIS_MUST_NOT_LEAK"
    notifier = TelegramNotifier(token=secret_token, chat_id="42")

    with caplog.at_level(logging.DEBUG):
        async with respx.mock(base_url=TELEGRAM_API_BASE) as mock:
            mock.post(f"/bot{secret_token}/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True}),
            )
            await notifier.notify("runner.started", strategy="x", symbols=[], dry_run=True)

    leaked = [r for r in caplog.records if secret_token in r.getMessage()]
    assert leaked == [], (
        f"토큰 노출 회귀 — {len(leaked)}건의 로그에 토큰이 포함됨: "
        f"{[r.name + ':' + r.levelname for r in leaked]}"
    )


async def test_kiwoom_url_not_logged_at_info(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """키움도 같은 httpx 인스턴스 → INFO 강등으로 URL 라인 자체가 안 떠야."""
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.setenv("KIWOOM_APP_KEY", "k")
    monkeypatch.setenv("KIWOOM_APP_SECRET", "s")
    cli_module.get_settings.cache_clear()  # type: ignore[attr-defined]
    cli_module._setup_logging()

    httpx_records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.name in ("httpx", "httpcore") and record.levelno < logging.WARNING:
                httpx_records.append(record)

    handler = _Capture()
    logging.getLogger("httpx").addHandler(handler)
    logging.getLogger("httpcore").addHandler(handler)

    try:
        async with httpx.AsyncClient() as client:
            with respx.mock(base_url="https://example.test") as mock:
                mock.get("/probe").mock(
                    return_value=httpx.Response(200, json={"ok": True}),
                )
                await client.get("https://example.test/probe")
    finally:
        logging.getLogger("httpx").removeHandler(handler)
        logging.getLogger("httpcore").removeHandler(handler)

    assert httpx_records == [], (
        "httpx INFO/DEBUG 가 빠져나옴 — URL 노출 회귀: "
        f"{[(r.name, r.levelname, r.getMessage()) for r in httpx_records]}"
    )


def test_token_never_leaked_in_repr_after_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """기존 마스킹 회귀(repr) 가 _setup_logging 후에도 유지."""
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.setenv("KIWOOM_APP_KEY", "k")
    monkeypatch.setenv("KIWOOM_APP_SECRET", "s")
    cli_module.get_settings.cache_clear()  # type: ignore[attr-defined]
    cli_module._setup_logging()

    n = TelegramNotifier(token="1234:DEADBEEFTOKEN", chat_id="42")
    assert "DEADBEEFTOKEN" not in repr(n)


def test_setup_logging_invoked_via_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    """typer callback 진입 시 자동 호출 — 모든 명령 보호."""
    from typer.testing import CliRunner

    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.setenv("KIWOOM_APP_KEY", "k")
    monkeypatch.setenv("KIWOOM_APP_SECRET", "s")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    cli_module.get_settings.cache_clear()  # type: ignore[attr-defined]

    # 가장 가벼운 명령 — quote 는 KiwoomRestClient 호출이 들어가지만 patch 로 차단.
    with patch("auto_trading.cli.get_quote") as mock_quote:
        mock_quote.return_value = type(
            "Q",
            (),
            {"price": 1, "change": 0, "change_pct": 0.0, "volume": 1, "ts": "2026-05-09"},
        )()
        # 실제 호출은 KiwoomRestClient 까지 가지 않게 mock_quote 가 await 안 받음 → 그냥
        # callback 만 호출됐는지 검증하자. quote 명령 자체는 빠르게 fail 해도 OK.
        with contextlib.suppress(json.JSONDecodeError):
            CliRunner().invoke(cli_module.app, ["--help"])

    # callback 이 호출됐다면 httpx 가 WARNING 으로 강등돼 있어야.
    assert logging.getLogger("httpx").level == logging.WARNING
