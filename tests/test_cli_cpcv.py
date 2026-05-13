"""auto-trade backtest --cpcv CLI — 29단계 PR37 (#2 Phase 1 CLI 결합)."""

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
from auto_trading.cli import _parse_cpcv_spec, app


@pytest.fixture(autouse=True)
def _reset_structlog() -> Generator[None]:
    yield
    logging.getLogger().handlers.clear()
    structlog.reset_defaults()


def _make_bars(n: int, base: float = 10_000.0) -> list[Bar]:
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


# ---- _parse_cpcv_spec ------------------------------------------------------


def test_parse_cpcv_spec_n_only() -> None:
    assert _parse_cpcv_spec("10") == (10, 0)


def test_parse_cpcv_spec_n_slash_embargo() -> None:
    assert _parse_cpcv_spec("10/2") == (10, 2)


def test_parse_cpcv_spec_rejects_invalid() -> None:
    import typer

    with pytest.raises(typer.BadParameter):
        _parse_cpcv_spec("10/2/3")  # 너무 많은 부분
    with pytest.raises(typer.BadParameter):
        _parse_cpcv_spec("abc")
    with pytest.raises(typer.BadParameter):
        _parse_cpcv_spec("1")  # n_folds < 2
    with pytest.raises(typer.BadParameter):
        _parse_cpcv_spec("10/-1")  # 음수 embargo


# ---- CLI 통합 ---------------------------------------------------------------


def test_backtest_cpcv_outputs_folds_table(isolated_cwd: Path) -> None:
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
                "--cpcv",
                "10",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "CPCV" in result.output
    assert "Summary" in result.output
    # 분포 요약 행 노출.
    assert "Sharpe p05" in result.output
    assert "PSR 평균" in result.output


def test_backtest_cpcv_with_embargo(isolated_cwd: Path) -> None:
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
                "--cpcv",
                "10/2",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "embargo=2" in result.output


def test_backtest_cpcv_saves_summary_and_folds(isolated_cwd: Path) -> None:
    bars = _make_bars(120)
    out = isolated_cwd / "cpcv-out"
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
                "--cpcv",
                "10",
                "--out",
                str(out),
            ],
        )

    assert result.exit_code == 0, result.output
    assert (out / "summary.json").exists()
    assert (out / "folds.csv").exists()
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["n_folds_requested"] == 10
    assert summary["embargo_bars"] == 0
    assert "sharpe_p05" in summary
    assert "psr_mean" in summary


def test_backtest_cpcv_rejects_combo_with_walk_forward(isolated_cwd: Path) -> None:
    """--cpcv 와 --walk-forward 는 상호 배타."""
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
                "--cpcv",
                "5",
                "--walk-forward",
                "30/30",
            ],
        )
    assert result.exit_code != 0
    assert "동시" in result.output


def test_backtest_cpcv_zero_folds_exits_one(isolated_cwd: Path) -> None:
    """데이터 너무 적어 fold 0건이면 exit 1 + 안내."""
    # n_folds=10 이지만 4 봉만 → ValueError (fold_size 너무 작음). typer 가 그걸 exit 1 로 변환.
    bars = _make_bars(4)
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
                "--cpcv",
                "10",
            ],
        )
    assert result.exit_code != 0
