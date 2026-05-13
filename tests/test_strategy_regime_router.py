"""레짐 → 프로파일 경로 라우팅 — 29단계 PR40 (#5)."""

from __future__ import annotations

from pathlib import Path

from auto_trading.strategy.regime_router import (
    REGIME_PROFILE_MAP,
    select_profile_for_regime,
)
from auto_trading.strategy.scanner import MarketRegime


def _make_profiles(root: Path, names: list[str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for n in names:
        (root / f"{n}.yaml").write_text("risk: {}\n", encoding="utf-8")


def test_routes_each_regime_to_mapped_profile(tmp_path: Path) -> None:
    _make_profiles(
        tmp_path,
        ["daytrade-trend", "daytrade-defensive", "daytrade-range", "daytrade-crash"],
    )
    for regime, expected_name in REGIME_PROFILE_MAP.items():
        p = select_profile_for_regime(regime, profiles_root=tmp_path)
        assert p is not None
        assert p.name == f"{expected_name}.yaml"


def test_falls_back_when_mapped_missing(tmp_path: Path) -> None:
    """매핑된 파일이 없으면 fallback 사용."""
    # 매핑 파일 없음, fallback 만 존재.
    fallback = tmp_path / "daytrade-default.yaml"
    fallback.write_text("risk: {}\n", encoding="utf-8")
    p = select_profile_for_regime(MarketRegime.TREND_UP, profiles_root=tmp_path, fallback=fallback)
    assert p == fallback


def test_returns_none_when_no_match_and_no_fallback(tmp_path: Path) -> None:
    p = select_profile_for_regime(MarketRegime.TREND_UP, profiles_root=tmp_path)
    assert p is None


def test_returns_none_when_fallback_missing(tmp_path: Path) -> None:
    """fallback 경로도 존재하지 않으면 None."""
    fallback = tmp_path / "missing.yaml"
    p = select_profile_for_regime(MarketRegime.RANGE, profiles_root=tmp_path, fallback=fallback)
    assert p is None


def test_prefers_mapped_over_fallback(tmp_path: Path) -> None:
    """매핑된 파일이 존재하면 fallback 무시."""
    _make_profiles(tmp_path, ["daytrade-trend"])
    fallback = tmp_path / "daytrade-default.yaml"
    fallback.write_text("risk: {}\n", encoding="utf-8")
    p = select_profile_for_regime(MarketRegime.TREND_UP, profiles_root=tmp_path, fallback=fallback)
    assert p is not None
    assert p.name == "daytrade-trend.yaml"


def test_regime_profile_map_covers_all_regimes() -> None:
    """REGIME_PROFILE_MAP 가 MarketRegime 전체를 커버 — 누락 시 라우팅 실패."""
    assert set(REGIME_PROFILE_MAP.keys()) == set(MarketRegime)
