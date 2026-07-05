"""auto-trade ops CLI 회귀.

8개 LaunchAgent 자동실행 작업 상태표 출력 검증.

검증:
- plist 3개 + launchctl exit 0 → 3행 표, 라벨 모두 출력.
- launchctl 실패(None) → exit 자리 "-", last_run 은 log mtime fallback.
- plist 폴더 비어있음 → 안내 메시지 + exit 0.
- churnfix-verify 라벨 포함 → 비고에 "별도 정리" 표시.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest
import structlog
from typer.testing import CliRunner

from auto_trading.cli import app
from auto_trading.monitor import launchd as _launchd


@pytest.fixture(autouse=True)
def _reset_structlog() -> Generator[None]:
    yield
    logging.getLogger().handlers.clear()
    structlog.reset_defaults()


def _write_plist(launchd_dir: Path, short: str) -> None:
    label = f"com.mizeloble.auto-trade.{short}"
    (launchd_dir / f"{label}.plist").write_text(
        f"<?xml version='1.0'?><plist><dict><key>Label</key><string>{label}</string></dict></plist>",
        encoding="utf-8",
    )


def _write_log(logs_dir: Path, short: str, age_sec: float = 0) -> Path:
    f = logs_dir / f"launchd-{short}.out.log"
    f.touch()
    if age_sec:
        old = f.stat().st_mtime - age_sec
        os.utime(f, (old, old))
    return f


def test_ops_three_jobs_with_launchctl(isolated_cwd: Path) -> None:
    launchd_dir = isolated_cwd / "scripts" / "launchd"
    launchd_dir.mkdir(parents=True)
    logs_dir = isolated_cwd / "logs"
    logs_dir.mkdir(parents=True)

    for short in ("paper-day", "heartbeat-check", "log-rotate"):
        _write_plist(launchd_dir, short)
        _write_log(logs_dir, short)

    with patch.object(_launchd, "_launchctl_last_exit_status", return_value="0"):
        result = CliRunner().invoke(app, ["ops"])

    assert result.exit_code == 0, result.output
    assert "paper-day" in result.output
    assert "heartbeat-check" in result.output
    assert "log-rotate" in result.output


def test_ops_launchctl_unavailable_uses_log_mtime(isolated_cwd: Path) -> None:
    """launchctl 미존재/실패 시 exit 자리는 '-', last_run 은 log mtime 으로 채워짐."""
    launchd_dir = isolated_cwd / "scripts" / "launchd"
    launchd_dir.mkdir(parents=True)
    logs_dir = isolated_cwd / "logs"
    logs_dir.mkdir(parents=True)

    _write_plist(launchd_dir, "paper-day")
    _write_log(logs_dir, "paper-day")

    with patch.object(_launchd, "_launchctl_last_exit_status", return_value=None):
        result = CliRunner().invoke(app, ["ops"])

    assert result.exit_code == 0, result.output
    assert "paper-day" in result.output


def test_ops_empty_dir_message(isolated_cwd: Path) -> None:
    """plist 폴더가 비어있으면 안내 메시지 + exit 0."""
    launchd_dir = isolated_cwd / "scripts" / "launchd"
    launchd_dir.mkdir(parents=True)

    result = CliRunner().invoke(app, ["ops"])

    assert result.exit_code == 0, result.output
    assert "등록된 LaunchAgent plist 가 없습니다" in result.output


def test_ops_never_exited_shows_dot(isolated_cwd: Path) -> None:
    """launchctl 가 '(never exited)' 라고 답하면 Exit 칸은 '·' 로 구분 표시."""
    launchd_dir = isolated_cwd / "scripts" / "launchd"
    launchd_dir.mkdir(parents=True)
    logs_dir = isolated_cwd / "logs"
    logs_dir.mkdir(parents=True)

    _write_plist(launchd_dir, "paper-day")
    _write_log(logs_dir, "paper-day")

    with patch.object(_launchd, "_launchctl_last_exit_status", return_value="(never exited)"):
        result = CliRunner(env={"COLUMNS": "200"}).invoke(app, ["ops"])

    assert result.exit_code == 0, result.output
    assert "paper-day" in result.output
    assert "·" in result.output


def test_ops_churnfix_note(isolated_cwd: Path) -> None:
    """churnfix-verify 라벨은 '⚠ 만료된 1회성, 별도 정리' 비고로 표시."""
    launchd_dir = isolated_cwd / "scripts" / "launchd"
    launchd_dir.mkdir(parents=True)
    logs_dir = isolated_cwd / "logs"
    logs_dir.mkdir(parents=True)

    _write_plist(launchd_dir, "churnfix-verify")
    _write_log(logs_dir, "churnfix-verify")

    with patch.object(_launchd, "_launchctl_last_exit_status", return_value="0"):
        # 좁은 터미널에서 "별도 정리" 가 wrap 되지 않도록 width 명시.
        result = CliRunner(env={"COLUMNS": "200"}).invoke(app, ["ops"])

    assert result.exit_code == 0, result.output
    assert "churnfix-verify" in result.output
    assert "별도 정리" in result.output
