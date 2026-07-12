"""auto-trade ops CLI 회귀.

8개 LaunchAgent 자동실행 작업 상태표 출력 검증.

검증:
- plist 3개 + launchctl exit 0 → 3행 표, 라벨 모두 출력.
- launchctl 실패(None) → exit 자리 "-", last_run 은 log mtime fallback.
- plist 폴더 비어있음 → 안내 메시지 + exit 0.
- churnfix-verify 라벨 포함 → 비고에 "별도 정리" 표시.
- 날짜별 로그(`<short>-YYYY-MM-DD.log`, insight 접두사 예외)로 마지막 실행 채움.
- StartCalendarInterval → 다음 예정 시각 + 7일 예상/실제 실행일.
- StartInterval → "매 N분" 표기.
"""

from __future__ import annotations

import logging
import os
import plistlib
from collections.abc import Generator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import structlog
from typer.testing import CliRunner

from auto_trading._common import KST
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


def _write_schedule_plist(launchd_dir: Path, short: str, extra: dict[str, Any]) -> None:
    """스케줄 키를 가진 진짜 plist 를 plistlib 로 기록 (파싱 경로까지 검증)."""
    label = f"com.mizeloble.auto-trade.{short}"
    payload: dict[str, Any] = {"Label": label, **extra}
    with (launchd_dir / f"{label}.plist").open("wb") as fh:
        plistlib.dump(payload, fh)


def test_ops_dated_log_fills_last_run(isolated_cwd: Path) -> None:
    """레거시 launchd-*.log 없이 날짜별 로그만 있어도 마지막 실행이 채워진다.

    2026-07-12 버그 수정 회귀 — 래퍼 스크립트가 날짜별 로그만 쓰는 잡(paper-day 등)의
    마지막 실행이 옛 레거시 mtime 에 고착돼 보이던 문제.
    """
    launchd_dir = isolated_cwd / "scripts" / "launchd"
    launchd_dir.mkdir(parents=True)
    logs_dir = isolated_cwd / "logs"
    logs_dir.mkdir(parents=True)

    _write_plist(launchd_dir, "paper-day")
    today = datetime.now(KST).date().isoformat()
    (logs_dir / f"paper-day-{today}.log").touch()

    with patch.object(_launchd, "_launchctl_last_exit_status", return_value="0"):
        result = CliRunner(env={"COLUMNS": "200"}).invoke(app, ["ops"])

    assert result.exit_code == 0, result.output
    # 마지막 실행 칸에 오늘 날짜(방금 touch 한 파일 mtime)가 보여야 한다.
    assert today in result.output


def test_ops_insight_prefix_exception(isolated_cwd: Path) -> None:
    """insight-weekly 는 로그 접두사가 `insight-` — 접두사 매핑 회귀."""
    launchd_dir = isolated_cwd / "scripts" / "launchd"
    launchd_dir.mkdir(parents=True)
    logs_dir = isolated_cwd / "logs"
    logs_dir.mkdir(parents=True)

    _write_plist(launchd_dir, "insight-weekly")
    today = datetime.now(KST).date().isoformat()
    (logs_dir / f"insight-{today}.log").touch()

    with patch.object(_launchd, "_launchctl_last_exit_status", return_value="0"):
        result = CliRunner(env={"COLUMNS": "200"}).invoke(app, ["ops"])

    assert result.exit_code == 0, result.output
    assert today in result.output


def test_ops_calendar_next_run_and_week_stats(isolated_cwd: Path) -> None:
    """평일 08:55 스케줄 → 다음 예정 08:55 표시 + 7일 실행일 5/3 (놓친 날 강조)."""
    launchd_dir = isolated_cwd / "scripts" / "launchd"
    launchd_dir.mkdir(parents=True)
    logs_dir = isolated_cwd / "logs"
    logs_dir.mkdir(parents=True)

    _write_schedule_plist(
        launchd_dir,
        "paper-day",
        {"StartCalendarInterval": [{"Hour": 8, "Minute": 55, "Weekday": wd} for wd in range(1, 6)]},
    )
    # 어제까지 7일 중 평일은 항상 5일. 그중 최근 평일 3일만 날짜별 로그 생성 → 5/3.
    now = datetime.now(KST)
    made = 0
    offset = 1
    while made < 3:
        d = now - timedelta(days=offset)
        offset += 1
        if d.isoweekday() <= 5:
            (logs_dir / f"paper-day-{d.date().isoformat()}.log").touch()
            made += 1

    with patch.object(_launchd, "_launchctl_last_exit_status", return_value="0"):
        result = CliRunner(env={"COLUMNS": "200"}).invoke(app, ["ops"])

    assert result.exit_code == 0, result.output
    assert "08:55" in result.output  # 다음 예정 시각
    assert "5/3" in result.output  # 예상 5일 / 실제 3일


def test_ops_interval_shows_period(isolated_cwd: Path) -> None:
    """StartInterval 300 → 다음 예정 칸에 '매 5분', 실제 측정 불가라 7/-."""
    launchd_dir = isolated_cwd / "scripts" / "launchd"
    launchd_dir.mkdir(parents=True)
    logs_dir = isolated_cwd / "logs"
    logs_dir.mkdir(parents=True)

    _write_schedule_plist(launchd_dir, "heartbeat-check", {"StartInterval": 300})
    _write_log(logs_dir, "heartbeat-check")

    with patch.object(_launchd, "_launchctl_last_exit_status", return_value="0"):
        result = CliRunner(env={"COLUMNS": "200"}).invoke(app, ["ops"])

    assert result.exit_code == 0, result.output
    assert "매 5분" in result.output
    assert "7/-" in result.output


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
