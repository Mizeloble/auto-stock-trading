"""DaytradeProfile YAML 라운드트립 + 검증 단위."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from auto_trading.daytrade.profile import (
    DaytradeProfile,
    DaytradeRiskLimits,
    DaytradeStrategyParams,
)


def test_profile_default_values_are_inactive() -> None:
    """기본값 — 모든 한도 비활성, threshold 0."""
    p = DaytradeProfile(name="t")
    assert p.strategy == "index_momentum_daytrade"
    assert p.top_n == 3
    assert p.exclude_etf is True
    assert p.params.cooldown_min == 30
    assert p.params.momentum_threshold_pct == 0.0
    assert p.risk.stop_loss_pct == 0.0
    assert p.risk.daily_loss_kill is False


def test_profile_round_trip_yaml(tmp_path: Path) -> None:
    """dump → load 라운드트립으로 모든 필드 보존."""
    p = DaytradeProfile(
        name="round trip",
        strategy="index_momentum_daytrade",
        top_n=2,
        exclude_etf=False,
        liquidate="14:50",
        params=DaytradeStrategyParams(
            cooldown_min=10, momentum_lookback_min=3, momentum_threshold_pct=0.5
        ),
        risk=DaytradeRiskLimits(
            stop_loss_pct=0.012,
            trailing_stop_pct=0.006,
            partial_take_pct=0.018,
            partial_take_qty_ratio=0.4,
            max_order_value=1_000_000,
            daily_notional_limit=5_000_000,
            daily_loss_kill=True,
        ),
        notes="hi",
    )
    path = tmp_path / "p.yaml"
    p.dump_yaml(path)
    loaded = DaytradeProfile.load_yaml(path)
    assert loaded == p


def test_profile_top_n_upper_bound() -> None:
    """top_n > 10 거절."""
    with pytest.raises(ValidationError):
        DaytradeProfile(name="x", top_n=99)


def test_profile_stop_loss_upper_bound() -> None:
    """stop_loss_pct > 0.10 (10%) 거절 — 보수 가드."""
    with pytest.raises(ValidationError):
        DaytradeProfile(name="x", risk=DaytradeRiskLimits(stop_loss_pct=0.5))


def test_profile_invalid_liquidate_format() -> None:
    """liquidate 가 'HH:MM' 아니면 거절."""
    with pytest.raises(ValidationError):
        DaytradeProfile(name="x", liquidate="15-15")


def test_profile_invalid_liquidate_range() -> None:
    """시간 범위 벗어나면 거절."""
    with pytest.raises(ValidationError):
        DaytradeProfile(name="x", liquidate="25:00")


def test_profile_partial_take_qty_ratio_capped() -> None:
    """ratio > 1.0 거절."""
    with pytest.raises(ValidationError):
        DaytradeProfile(name="x", risk=DaytradeRiskLimits(partial_take_qty_ratio=1.5))


def test_default_yaml_sample_loads() -> None:
    """저장소 샘플 strategies/profiles/daytrade-default.yaml 가 로드된다."""
    sample = (
        Path(__file__).resolve().parent.parent / "strategies" / "profiles" / "daytrade-default.yaml"
    )
    p = DaytradeProfile.load_yaml(sample)
    assert p.name
    assert p.strategy in ("index_momentum_daytrade", "intraday_momentum")
