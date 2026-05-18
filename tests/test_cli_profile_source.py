"""D — CLI 일관성: 상호배타 검증 공유 + -f 단축 + --symbol 별칭."""

from __future__ import annotations

from pathlib import Path

import pytest
import typer
from typer.main import get_command

from auto_trading.cli import app
from auto_trading.cli._profile_io import validate_strategy_source


def _option_decls(command_name: str) -> set[str]:
    """등록된 click 명령의 옵션 선언(`--foo`/`-f`) 집합.

    rich Typer help 문자열을 grep 하면 터미널 폭·Python 버전별 렌더 차이로
    플래그가 `--s…` 처럼 잘려 단언이 비결정적이다(2026-05-18 CI 회귀, py3.14
    final). 명령 객체의 param 선언을 직접 읽어 렌더와 무관하게 검증한다.
    """
    sub = get_command(app).commands[command_name]  # type: ignore[attr-defined]
    decls: set[str] = set()
    for p in sub.params:
        decls.update(getattr(p, "opts", []) or [])
        decls.update(getattr(p, "secondary_opts", []) or [])
    return decls


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
    assert "-f" in _option_decls("backtest")


def test_paper_help_lists_symbol_alias() -> None:
    assert "--symbol" in _option_decls("paper")
