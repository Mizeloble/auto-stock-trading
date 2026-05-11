"""monitor.data_integrity 단위 회귀.

29단계 PR22 (#19).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Generator
from pathlib import Path

import pytest
import structlog
from typer.testing import CliRunner

from auto_trading.cli import app
from auto_trading.monitor.data_integrity import (
    check_daily_reports,
    check_order_journal,
    severity_counts,
)


@pytest.fixture(autouse=True)
def _reset_structlog() -> Generator[None]:
    yield
    logging.getLogger().handlers.clear()
    structlog.reset_defaults()


# ---- check_daily_reports ----------------------------------------------------


def test_no_reports_dir_returns_empty(tmp_path: Path) -> None:
    """폴더 없으면 검사 대상 0 — 빈 리스트."""
    assert check_daily_reports(tmp_path / "missing") == []


def test_valid_reports_yield_no_issues(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "daily-2026-05-10.json").write_text(
        json.dumps({"date": "2026-05-10", "realized_pnl_pct": -1.5}),
        encoding="utf-8",
    )
    assert check_daily_reports(reports) == []


def test_invalid_json_reports_issue(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "daily-2026-05-10.json").write_text("not json", encoding="utf-8")
    issues = check_daily_reports(reports)
    assert len(issues) == 1
    assert issues[0].kind == "json_decode"


def test_empty_file_yields_warning(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "daily-2026-05-10.json").write_text("", encoding="utf-8")
    [issue] = check_daily_reports(reports)
    assert issue.kind == "empty_file"
    assert issue.severity == "warning"


def test_missing_date_field(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "daily-2026-05-10.json").write_text(
        json.dumps({"realized_pnl_pct": 0}), encoding="utf-8"
    )
    issues = check_daily_reports(reports)
    assert any(i.kind == "missing_field" for i in issues)


def test_filename_date_mismatch(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "daily-2026-05-10.json").write_text(
        json.dumps({"date": "2026-05-99", "realized_pnl_pct": 0}),
        encoding="utf-8",
    )
    issues = check_daily_reports(reports)
    assert any(i.kind == "invalid_value" and "불일치" in i.detail for i in issues)


def test_abnormal_pnl_pct_flagged(tmp_path: Path) -> None:
    """realized_pnl_pct 가 범위 밖 (예: 200% 또는 -500%) → 단위 혼동 의심."""
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "daily-2026-05-10.json").write_text(
        json.dumps({"date": "2026-05-10", "realized_pnl_pct": 200}),
        encoding="utf-8",
    )
    issues = check_daily_reports(reports)
    assert any(i.kind == "invalid_value" and "비정상" in i.detail for i in issues)


def test_non_dict_top_level(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "daily-2026-05-10.json").write_text(
        json.dumps(["array", "not", "dict"]), encoding="utf-8"
    )
    [issue] = check_daily_reports(reports)
    assert issue.kind == "missing_field"


# ---- check_order_journal -----------------------------------------------------


def test_no_journal_returns_empty(tmp_path: Path) -> None:
    assert check_order_journal(tmp_path / "no.jsonl") == []


def test_valid_journal_no_issues(tmp_path: Path) -> None:
    path = tmp_path / "state.jsonl"
    path.write_text(
        json.dumps({"event": "submit", "client_id": "c1", "symbol": "005930"})
        + "\n"
        + json.dumps({"event": "accepted", "client_id": "c1", "order_no": "X"})
        + "\n",
        encoding="utf-8",
    )
    assert check_order_journal(path) == []


def test_corrupt_line_flagged(tmp_path: Path) -> None:
    path = tmp_path / "state.jsonl"
    path.write_text(
        json.dumps({"event": "submit", "client_id": "c1"})
        + "\n"
        + "이건 JSON 아님\n"
        + json.dumps({"event": "accepted", "client_id": "c1"})
        + "\n",
        encoding="utf-8",
    )
    issues = check_order_journal(path)
    assert len(issues) == 1
    assert issues[0].kind == "line_corrupt"
    assert "line 2" in issues[0].detail


def test_missing_event_field(tmp_path: Path) -> None:
    path = tmp_path / "state.jsonl"
    path.write_text(json.dumps({"client_id": "c1", "symbol": "X"}) + "\n", encoding="utf-8")
    [issue] = check_order_journal(path)
    assert issue.kind == "missing_field"


def test_unknown_event_warning(tmp_path: Path) -> None:
    path = tmp_path / "state.jsonl"
    path.write_text(json.dumps({"event": "annotate", "client_id": "c1"}) + "\n", encoding="utf-8")
    [issue] = check_order_journal(path)
    assert issue.kind == "invalid_value"
    assert issue.severity == "warning"


# ---- severity_counts --------------------------------------------------------


def test_severity_counts_zero_when_empty() -> None:
    assert severity_counts([]) == (0, 0)


# ---- CLI --------------------------------------------------------------------


def test_data_check_clean_exits_zero(isolated_cwd: Path) -> None:
    """검사 대상 없거나 모두 통과 → exit 0 + 통과 메시지."""
    result = CliRunner().invoke(
        app,
        [
            "data",
            "check",
            "--reports-root",
            str(isolated_cwd / "reports"),
            "--journal",
            str(isolated_cwd / "state.jsonl"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "정합성 통과" in result.output


def test_data_check_errors_exit_one(isolated_cwd: Path) -> None:
    reports = isolated_cwd / "reports"
    reports.mkdir()
    (reports / "daily-2026-05-10.json").write_text("garbage", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "data",
            "check",
            "--reports-root",
            str(reports),
            "--journal",
            str(isolated_cwd / "no.jsonl"),
        ],
    )
    assert result.exit_code == 1, result.output
    assert "error" in result.output
    assert "json_decode" in result.output


def test_data_check_warnings_only_exit_zero(isolated_cwd: Path) -> None:
    """warning 만 있고 error 없으면 exit 0 (운영 정보용)."""
    reports = isolated_cwd / "reports"
    reports.mkdir()
    # 빈 파일 → warning.
    (reports / "daily-2026-05-10.json").write_text("", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "data",
            "check",
            "--reports-root",
            str(reports),
            "--journal",
            str(isolated_cwd / "no.jsonl"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "warning" in result.output
