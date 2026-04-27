"""StrategyProfile YAML 라운드트립 + 검증."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

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
