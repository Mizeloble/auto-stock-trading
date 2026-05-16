"""D — CLI 일관성: 상호배타 검증 공유 + -f 단축 + --symbol 별칭."""

from __future__ import annotations

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from auto_trading.cli import app
from auto_trading.cli._profile_io import validate_strategy_source


def test_validate_rejects_profile_with_strategy() -> None:
    with pytest.raises(typer.BadParameter):
        validate_strategy_source(profile=Path("p.yaml"), strategy="sma_crossover", symbol="005930")


def test_validate_rejects_neither_given() -> None:
    with pytest.raises(typer.BadParameter):
        validate_strategy_source(profile=None, strategy=None, symbol=None)


def test_validate_accepts_profile_only() -> None:
    validate_strategy_source(profile=Path("p.yaml"), strategy=None, symbol=None)


def test_validate_accepts_strategy_plus_symbol() -> None:
    validate_strategy_source(profile=None, strategy="sma_crossover", symbol="005930")


def test_backtest_help_lists_short_from_flag() -> None:
    result = CliRunner().invoke(app, ["backtest", "--help"])
    assert result.exit_code == 0
    assert "-f" in result.output


def test_paper_help_lists_symbol_alias() -> None:
    result = CliRunner().invoke(app, ["paper", "--help"])
    assert result.exit_code == 0
    assert "--symbol" in result.output
