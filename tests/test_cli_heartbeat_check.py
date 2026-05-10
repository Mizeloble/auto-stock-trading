"""auto-trade heartbeat-check CLI 회귀.

29단계 PR3 (#13).

검증:
- 파일 신선 → exit 0, 'heartbeat OK', notifier 호출 X.
- 파일 없음 → exit 0, 'missing', notifier 호출 1회 + cooldown 파일 생성.
- 파일 stale → exit 0, 'stale', notifier 호출 1회 + cooldown 파일 생성.
- cooldown 활성 (직전 알림 30분 안) → notifier 호출 X.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import structlog
from typer.testing import CliRunner

from auto_trading import cli as cli_module
from auto_trading.cli import app


@pytest.fixture(autouse=True)
def _reset_structlog() -> Generator[None]:
    yield
    logging.getLogger().handlers.clear()
    structlog.reset_defaults()


def test_heartbeat_check_ok_silent(isolated_cwd: Path) -> None:
    hb = isolated_cwd / "data" / "heartbeat" / "runner.txt"
    hb.parent.mkdir(parents=True, exist_ok=True)
    hb.write_text("now\n", encoding="utf-8")

    fake_notifier = AsyncMock()
    with patch.object(cli_module, "build_notifier", return_value=fake_notifier):
        result = CliRunner().invoke(app, ["heartbeat-check"])

    assert result.exit_code == 0, result.output
    assert "heartbeat OK" in result.output
    fake_notifier.notify.assert_not_called()


def test_heartbeat_check_missing_alerts_and_creates_cooldown(isolated_cwd: Path) -> None:
    fake_notifier = AsyncMock()
    with patch.object(cli_module, "build_notifier", return_value=fake_notifier):
        result = CliRunner().invoke(app, ["heartbeat-check"])

    assert result.exit_code == 0, result.output
    assert "missing" in result.output
    fake_notifier.notify.assert_awaited_once()
    args, kwargs = fake_notifier.notify.call_args
    assert args[0] == "runner.heartbeat_alert"
    assert kwargs["reason"] == "missing"
    assert (isolated_cwd / "data" / "heartbeat" / "last_alert.txt").exists()


def test_heartbeat_check_stale_alerts(isolated_cwd: Path) -> None:
    hb = isolated_cwd / "data" / "heartbeat" / "runner.txt"
    hb.parent.mkdir(parents=True, exist_ok=True)
    hb.write_text("old\n", encoding="utf-8")
    # mtime 을 2시간 전으로.
    old = hb.stat().st_mtime - 7200
    os.utime(hb, (old, old))

    fake_notifier = AsyncMock()
    with patch.object(cli_module, "build_notifier", return_value=fake_notifier):
        result = CliRunner().invoke(app, ["heartbeat-check", "--max-age-sec", "3600"])

    assert result.exit_code == 0, result.output
    assert "stale" in result.output
    fake_notifier.notify.assert_awaited_once()
    _, kwargs = fake_notifier.notify.call_args
    assert kwargs["reason"] == "stale"


def test_heartbeat_check_cooldown_silences_repeat_alert(isolated_cwd: Path) -> None:
    """직전 알림 5분 전 → cooldown 30분 안이라 notifier 호출 X."""
    cooldown = isolated_cwd / "data" / "heartbeat" / "last_alert.txt"
    cooldown.parent.mkdir(parents=True, exist_ok=True)
    cooldown.touch()

    fake_notifier = AsyncMock()
    with patch.object(cli_module, "build_notifier", return_value=fake_notifier):
        result = CliRunner().invoke(app, ["heartbeat-check"])

    assert result.exit_code == 0, result.output
    assert "cooldown 중" in result.output
    fake_notifier.notify.assert_not_called()


def test_heartbeat_check_cooldown_expired_re_alerts(isolated_cwd: Path) -> None:
    """cooldown 파일 mtime 이 cooldown_sec 보다 오래되면 알림 다시 발송."""
    cooldown = isolated_cwd / "data" / "heartbeat" / "last_alert.txt"
    cooldown.parent.mkdir(parents=True, exist_ok=True)
    cooldown.touch()
    old = cooldown.stat().st_mtime - 3600  # 1시간 전 알림
    os.utime(cooldown, (old, old))

    fake_notifier = AsyncMock()
    with patch.object(cli_module, "build_notifier", return_value=fake_notifier):
        result = CliRunner().invoke(app, ["heartbeat-check", "--cooldown-sec", "1800"])

    assert result.exit_code == 0, result.output
    fake_notifier.notify.assert_awaited_once()
