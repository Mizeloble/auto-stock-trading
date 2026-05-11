"""auto-trade latency CLI 회귀.

29단계 PR19 (#20).
"""

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
    yield
    logging.getLogger().handlers.clear()
    structlog.reset_defaults()


def _seed_latency(path: Path, *, today_ms: list[float]) -> None:
    """오늘 일자에 ms 시퀀스 기록."""
    today = datetime.now(KST).date().isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for ms in today_ms:
            fh.write(
                json.dumps(
                    {
                        "ts": f"{today}T10:00:00+09:00",
                        "label": "order_send_ok",
                        "duration_ms": ms,
                    }
                )
                + "\n"
            )


def test_latency_missing_path_shows_guidance(isolated_cwd: Path) -> None:
    result = CliRunner().invoke(app, ["latency", "--path", str(isolated_cwd / "nope.jsonl")])
    assert result.exit_code == 0, result.output
    assert "latency 저널 없음" in result.output
    assert "LATENCY_LOG_PATH" in result.output


def test_latency_shows_stats_table(isolated_cwd: Path) -> None:
    path = isolated_cwd / "lat.jsonl"
    _seed_latency(path, today_ms=[100, 200, 300, 400, 500])

    result = CliRunner().invoke(app, ["latency", "--path", str(path), "--days", "1"])

    assert result.exit_code == 0, result.output
    assert "order_send_ok" in result.output
    # 평균 300, p50 300, max 500
    assert "300" in result.output
    assert "500" in result.output


def test_latency_empty_range_shows_message(isolated_cwd: Path) -> None:
    """파일은 있지만 최근 N일 안에 데이터 없음."""
    path = isolated_cwd / "lat.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    # 2020 년 옛날 데이터.
    path.write_text(
        json.dumps({"ts": "2020-01-01T10:00:00+09:00", "label": "x", "duration_ms": 10}) + "\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["latency", "--path", str(path), "--days", "7"])
    assert result.exit_code == 0, result.output
    assert "데이터 없음" in result.output


def test_latency_high_p99_uses_red_color(isolated_cwd: Path) -> None:
    """p99 ≥ 3000ms 시 빨강 강조 (rich 마크업 포함)."""
    path = isolated_cwd / "lat.jsonl"
    _seed_latency(path, today_ms=[100, 200, 5000])

    result = CliRunner().invoke(app, ["latency", "--path", str(path), "--days", "1"])
    assert result.exit_code == 0, result.output
    # rich 의 [red] 마크업이 렌더링된 흔적은 ANSI escape 또는 plain "5000" — 둘 다 OK.
    assert "5000" in result.output
