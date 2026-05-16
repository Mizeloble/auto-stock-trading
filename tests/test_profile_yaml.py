"""StrategyProfile YAML 라운드트립 + 검증."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import typer
from pydantic import ValidationError

from auto_trading.cli._profile_io import (
    load_daytrade_profile_or_exit,
    load_strategy_profile_or_exit,
)
from auto_trading.strategy.profile import StrategyProfile


def _make_profile() -> StrategyProfile:
    return StrategyProfile(
        name="auto_005930",
        symbol="005930",
        strategy="rsi_reversion",
        params={"period": 14, "oversold": 30, "overbought": 70},
        weight=0.8,
        notes="박스권 → RSI 역추세",
        created_at=datetime(2026, 4, 26, 10, 15, tzinfo=UTC),
    )


def test_dump_load_roundtrip(tmp_path: Path) -> None:
    profile = _make_profile()
    path = tmp_path / "auto_005930.yaml"
    profile.dump_yaml(path)
    loaded = StrategyProfile.load_yaml(path)
    assert loaded == profile


def test_dump_writes_korean_header(tmp_path: Path) -> None:
    profile = _make_profile()
    path = tmp_path / "p.yaml"
    profile.dump_yaml(path)
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# auto-trade 자동 생성")
    assert "uv run auto-trade backtest" in text


def test_invalid_strategy_name_rejected() -> None:
    with pytest.raises(ValidationError):
        StrategyProfile(
            name="x",
            symbol="005930",
            strategy="unknown_strategy",  # type: ignore[arg-type]
            params={},
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )


def test_weight_out_of_range_rejected() -> None:
    with pytest.raises(ValidationError):
        StrategyProfile(
            name="x",
            symbol="005930",
            strategy="sma_crossover",
            params={"short": 5, "long": 20},
            weight=1.5,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )


def test_load_yaml_rejects_non_mapping(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(ValueError):
        StrategyProfile.load_yaml(path)


# ---- 초보 친화 오류 헬퍼 (cli/_profile_io) ----------------------------------


def test_friendly_loader_missing_file_exits_cleanly(tmp_path: Path) -> None:
    """경로 오타 → FileNotFoundError 트레이스백 대신 Exit(1)."""
    with pytest.raises(typer.Exit) as ei:
        load_strategy_profile_or_exit(tmp_path / "nope.yaml")
    assert ei.value.exit_code == 1


def test_friendly_loader_broken_yaml_exits_cleanly(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("name: [unclosed\n", encoding="utf-8")
    with pytest.raises(typer.Exit) as ei:
        load_strategy_profile_or_exit(path)
    assert ei.value.exit_code == 1


def test_friendly_loader_non_mapping_exits_cleanly(tmp_path: Path) -> None:
    path = tmp_path / "list.yaml"
    path.write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(typer.Exit) as ei:
        load_strategy_profile_or_exit(path)
    assert ei.value.exit_code == 1


def test_friendly_loader_validation_error_lists_keys(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    """필수 키 누락/범위 위반 → 검증 실패 헤더 + 키 표시, Exit(1)."""
    path = tmp_path / "invalid.yaml"
    path.write_text("name: x\nsymbol: '005930'\nstrategy: unknown_strategy\n", encoding="utf-8")
    with pytest.raises(typer.Exit) as ei:
        load_strategy_profile_or_exit(path)
    assert ei.value.exit_code == 1
    assert "프로파일 검증 실패" in capsys.readouterr().out


def test_friendly_loader_happy_path_returns_profile(tmp_path: Path) -> None:
    profile = _make_profile()
    path = tmp_path / "ok.yaml"
    profile.dump_yaml(path)
    assert load_strategy_profile_or_exit(path) == profile


def test_friendly_daytrade_loader_missing_file_exits(tmp_path: Path) -> None:
    with pytest.raises(typer.Exit) as ei:
        load_daytrade_profile_or_exit(tmp_path / "nope.yaml")
    assert ei.value.exit_code == 1
