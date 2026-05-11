"""auto-trade backtest-compare CLI 회귀.

29단계 PR20 (#23).
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


@pytest.fixture(autouse=True)
def _reset_structlog() -> Generator[None]:
    yield
    logging.getLogger().handlers.clear()
    structlog.reset_defaults()


def _seed_report(folder: Path, **fields: float | int) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    base = {
        "initial_equity": 10_000_000.0,
        "final_equity": 11_000_000.0,
        "total_return_pct": 10.00,
        "cagr_pct": 8.50,
        "sharpe": 1.200,
        "annualized_vol_pct": 12.00,
        "max_drawdown_pct": -5.00,
        "win_rate_pct": 55.00,
        "num_trades": 100,
        "num_round_trips": 40,
    }
    base.update(fields)
    (folder / "report.json").write_text(json.dumps(base), encoding="utf-8")


def test_compare_two_reports_shows_diff(tmp_path: Path) -> None:
    """기본 흐름 — 두 report 비교 + 차이 출력."""
    _seed_report(tmp_path / "baseline", sharpe=1.0, total_return_pct=8.0)
    _seed_report(tmp_path / "new", sharpe=1.5, total_return_pct=12.0)

    result = CliRunner().invoke(
        app,
        ["backtest-compare", str(tmp_path / "baseline"), str(tmp_path / "new")],
    )
    assert result.exit_code == 0, result.output
    assert "baseline" in result.output
    assert "Sharpe" in result.output
    # 차이가 +0.500 (Sharpe), +4.00 (total_return) 표시
    assert "+0.500" in result.output
    assert "+4.00" in result.output


def test_compare_missing_run1_exits_with_error(tmp_path: Path) -> None:
    """run1 폴더에 report.json 없음 → exit 1."""
    (tmp_path / "empty").mkdir()
    _seed_report(tmp_path / "new")

    result = CliRunner().invoke(
        app,
        ["backtest-compare", str(tmp_path / "empty"), str(tmp_path / "new")],
    )
    assert result.exit_code == 1
    assert "report.json 없음" in result.output


def test_compare_missing_run2_exits_with_error(tmp_path: Path) -> None:
    _seed_report(tmp_path / "baseline")
    (tmp_path / "empty").mkdir()

    result = CliRunner().invoke(
        app,
        ["backtest-compare", str(tmp_path / "baseline"), str(tmp_path / "empty")],
    )
    assert result.exit_code == 1


def test_compare_corrupt_report_exits_with_error(tmp_path: Path) -> None:
    (tmp_path / "baseline").mkdir()
    (tmp_path / "baseline" / "report.json").write_text("not json", encoding="utf-8")
    _seed_report(tmp_path / "new")

    result = CliRunner().invoke(
        app,
        ["backtest-compare", str(tmp_path / "baseline"), str(tmp_path / "new")],
    )
    assert result.exit_code == 1
    assert "파싱 실패" in result.output


def test_compare_handles_missing_fields_gracefully(tmp_path: Path) -> None:
    """report 에 일부 필드 누락 → 해당 행 skip, 나머지는 정상 표시."""
    minimal1 = tmp_path / "old"
    minimal1.mkdir()
    (minimal1 / "report.json").write_text(
        json.dumps({"sharpe": 1.0, "cagr_pct": 5.0}), encoding="utf-8"
    )
    minimal2 = tmp_path / "new"
    minimal2.mkdir()
    (minimal2 / "report.json").write_text(
        json.dumps({"sharpe": 1.2, "cagr_pct": 7.0}), encoding="utf-8"
    )

    result = CliRunner().invoke(
        app,
        ["backtest-compare", str(minimal1), str(minimal2)],
    )
    assert result.exit_code == 0, result.output
    assert "Sharpe" in result.output
    assert "CAGR" in result.output


def test_compare_mdd_improvement_marked_green(tmp_path: Path) -> None:
    """max_drawdown 은 음수 → 0 에 가까울수록 좋음. -7 → -3 = 차이 +4 (개선)."""
    _seed_report(tmp_path / "old", max_drawdown_pct=-7.0)
    _seed_report(tmp_path / "new", max_drawdown_pct=-3.0)

    result = CliRunner().invoke(
        app,
        ["backtest-compare", str(tmp_path / "old"), str(tmp_path / "new")],
    )
    assert result.exit_code == 0, result.output
    # 차이 +4.00 표시 (rich color 마크업은 CliRunner 출력에서 제거될 수도 있음).
    assert "+4.00" in result.output


def test_compare_sharpe_regression_marked_red(tmp_path: Path) -> None:
    """Sharpe 가 낮아짐 → 차이 음수, 빨강."""
    _seed_report(tmp_path / "old", sharpe=1.5)
    _seed_report(tmp_path / "new", sharpe=0.9)

    result = CliRunner().invoke(
        app,
        ["backtest-compare", str(tmp_path / "old"), str(tmp_path / "new")],
    )
    assert result.exit_code == 0, result.output
    assert "-0.600" in result.output
