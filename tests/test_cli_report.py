"""`auto-trade report` 명령 회귀."""

from __future__ import annotations

import json
import logging
from collections.abc import Generator
from datetime import datetime
from pathlib import Path

import pytest
import structlog
from typer.testing import CliRunner

from auto_trading._common import KST
from auto_trading.cli import app


@pytest.fixture(autouse=True)
def _reset_structlog() -> Generator[None]:
    """CLI 실행 사이의 structlog 글로벌 상태를 매 테스트마다 초기화."""
    yield
    logging.getLogger().handlers.clear()
    structlog.reset_defaults()


def _write_log(path: Path, *records: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps({"event": r["event"], **r}) for r in records),
        encoding="utf-8",
    )


def test_report_explicit_date_writes_json_and_prints(isolated_cwd: Path) -> None:
    log_dir = isolated_cwd / "logs"
    log_path = log_dir / "paper-day-2026-05-05.log"
    _write_log(
        log_path,
        {"event": "daytrade.discovery", "chosen": ["018880"], "blocked": []},
        {
            "event": "runner.order_simulated",
            "symbol": "018880",
            "side": "buy",
            "qty": 10,
            "price": 5000.0,
        },
        {"event": "runner.liquidate_simulated", "symbol": "018880", "qty": 10},
    )
    runner = CliRunner()
    result = runner.invoke(app, ["report", "--date", "2026-05-05"])
    assert result.exit_code == 0, result.output
    out_path = isolated_cwd / "data" / "reports" / "daily-2026-05-05.json"
    assert out_path.exists()
    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert data["discovery_chosen"] == ["018880"]
    assert data["mode"] == "day-trade"
    assert "📊 일일 리포트" in result.output
    assert "018880" in result.output


def test_report_missing_log_exits_with_error(isolated_cwd: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["report", "--date", "2026-01-01"])
    assert result.exit_code == 1
    assert "로그 파일 없음" in result.output


def test_report_default_date_uses_today_kst(isolated_cwd: Path) -> None:
    """--date 미지정 시 오늘 KST 일자의 로그를 본다."""
    today = datetime.now(KST).date().isoformat()
    log_path = isolated_cwd / "logs" / f"paper-day-{today}.log"
    _write_log(
        log_path,
        {"event": "daytrade.discovery", "chosen": ["A"], "blocked": []},
    )
    runner = CliRunner()
    result = runner.invoke(app, ["report"])
    assert result.exit_code == 0, result.output
    assert (isolated_cwd / "data" / "reports" / f"daily-{today}.json").exists()


def test_report_explicit_log_path_overrides_default(isolated_cwd: Path) -> None:
    """--log 로 임의 경로 지정 가능 — 운영 로그 외 임시 분석용."""
    custom = isolated_cwd / "tmp" / "custom.log"
    _write_log(custom, {"event": "daytrade.discovery", "chosen": ["B"], "blocked": []})
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["report", "--date", "2026-05-05", "--log", str(custom)],
    )
    assert result.exit_code == 0, result.output
    out_path = isolated_cwd / "data" / "reports" / "daily-2026-05-05.json"
    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert data["discovery_chosen"] == ["B"]


def test_report_custom_out_dir(isolated_cwd: Path) -> None:
    log_path = isolated_cwd / "logs" / "paper-day-2026-05-05.log"
    _write_log(log_path, {"event": "daytrade.discovery", "chosen": ["A"], "blocked": []})
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["report", "--date", "2026-05-05", "--out", str(isolated_cwd / "out")],
    )
    assert result.exit_code == 0, result.output
    assert (isolated_cwd / "out" / "daily-2026-05-05.json").exists()
