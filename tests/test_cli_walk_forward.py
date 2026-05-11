"""auto-trade backtest --walk-forward CLI 회귀.

29단계 PR24 (#1 2차).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Generator
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import structlog
from typer.testing import CliRunner

from auto_trading._common import KST, Bar
from auto_trading.cli import _parse_walk_forward_spec, app


@pytest.fixture(autouse=True)
def _reset_structlog() -> Generator[None]:
    yield
    logging.getLogger().handlers.clear()
    structlog.reset_defaults()


def _make_bars(n: int, base: float = 10_000.0) -> list[Bar]:
    """단조 증가 가격 n 봉."""
    now = datetime.now(KST).replace(hour=15, minute=30, second=0, microsecond=0)
    bars = []
    for i in range(n):
        ts = now - timedelta(days=n - 1 - i)
        price = base * (1 + 0.001 * i)
        bars.append(
            Bar(
                symbol="005930",
                ts=ts,
                open=price,
                high=price * 1.005,
                low=price * 0.995,
                close=price,
                volume=1_000,
            )
        )
    return bars


# ---- _parse_walk_forward_spec ----------------------------------------------


def test_parse_walk_forward_spec_simple() -> None:
    assert _parse_walk_forward_spec("90/30") == (90, 30)


def test_parse_walk_forward_spec_with_d_suffix() -> None:
    assert _parse_walk_forward_spec("90d/30d") == (90, 30)


def test_parse_walk_forward_spec_rejects_invalid_format() -> None:
    import typer

    with pytest.raises(typer.BadParameter):
        _parse_walk_forward_spec("90")
    with pytest.raises(typer.BadParameter):
        _parse_walk_forward_spec("abc/def")
    with pytest.raises(typer.BadParameter):
        _parse_walk_forward_spec("0/30")
    with pytest.raises(typer.BadParameter):
        _parse_walk_forward_spec("90/-10")


# ---- CLI 통합 ---------------------------------------------------------------


def test_backtest_walk_forward_outputs_windows_table(isolated_cwd: Path) -> None:
    bars = _make_bars(120)
    fake = AsyncMock(return_value=bars)
    with patch("auto_trading.cli.get_daily_bars", fake):
        result = CliRunner().invoke(
            app,
            [
                "backtest",
                "--strategy",
                "sma_crossover",
                "--symbol",
                "005930",
                "--from",
                bars[0].ts.date().isoformat(),
                "--walk-forward",
                "30/30",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "Walk-Forward" in result.output
    assert "Summary" in result.output
    assert "Sharpe 평균" in result.output


def test_backtest_walk_forward_saves_summary_json(isolated_cwd: Path) -> None:
    bars = _make_bars(120)
    out = isolated_cwd / "wf-out"
    fake = AsyncMock(return_value=bars)
    with patch("auto_trading.cli.get_daily_bars", fake):
        result = CliRunner().invoke(
            app,
            [
                "backtest",
                "--strategy",
                "sma_crossover",
                "--symbol",
                "005930",
                "--from",
                bars[0].ts.date().isoformat(),
                "--walk-forward",
                "30/30",
                "--out",
                str(out),
            ],
        )

    assert result.exit_code == 0, result.output
    assert (out / "summary.json").exists()
    assert (out / "windows.csv").exists()

    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["window_days"] == 30
    assert summary["step_days"] == 30
    assert summary["n_windows"] >= 1


def test_backtest_walk_forward_insufficient_data_exits_one(isolated_cwd: Path) -> None:
    """데이터가 한 윈도우도 못 만들면 exit 1."""
    bars = _make_bars(5)  # 5봉만
    fake = AsyncMock(return_value=bars)
    with patch("auto_trading.cli.get_daily_bars", fake):
        result = CliRunner().invoke(
            app,
            [
                "backtest",
                "--strategy",
                "sma_crossover",
                "--symbol",
                "005930",
                "--from",
                bars[0].ts.date().isoformat(),
                "--walk-forward",
                "90/30",
            ],
        )

    assert result.exit_code == 1
    assert "윈도우 0건" in result.output


def test_backtest_default_mode_unchanged_by_pr24(isolated_cwd: Path) -> None:
    """--walk-forward 없이는 기존 단일 백테스트 흐름 그대로."""
    bars = _make_bars(60)
    fake = AsyncMock(return_value=bars)
    with patch("auto_trading.cli.get_daily_bars", fake):
        result = CliRunner().invoke(
            app,
            [
                "backtest",
                "--strategy",
                "sma_crossover",
                "--symbol",
                "005930",
                "--from",
                bars[0].ts.date().isoformat(),
                "--out",
                str(isolated_cwd / "single"),
            ],
        )

    assert result.exit_code == 0, result.output
    assert "백테스트 결과" in result.output
    # Walk-Forward 헤더가 안 들어와야 함.
    assert "Walk-Forward" not in result.output
