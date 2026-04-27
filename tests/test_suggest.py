"""국면 → 추천 매핑 결정론 회귀."""

from __future__ import annotations

from datetime import UTC, datetime

from auto_trading.strategy import suggest
from auto_trading.strategy.scanner import MarketRegime, RegimeReport


def _report(regime: MarketRegime) -> RegimeReport:
    return RegimeReport(
        regime=regime,
        metrics={"slope_pct": 0.0, "atr_pct": 0.0, "bb_width_pct": 0.0, "last_close": 0.0},
        summary_ko="더미",
    )


def test_recommend_trend_up_uses_sma_crossover() -> None:
    profile = suggest.recommend(
        _report(MarketRegime.TREND_UP),
        symbol="005930",
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert profile.strategy == "sma_crossover"
    assert profile.params == {"short": 5, "long": 20}
    assert profile.weight == 1.0
    assert profile.symbol == "005930"
    assert profile.name == "auto_005930"


def test_recommend_range_uses_rsi_reversion() -> None:
    profile = suggest.recommend(
        _report(MarketRegime.RANGE),
        symbol="000660",
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert profile.strategy == "rsi_reversion"
    assert profile.params == {"period": 14, "oversold": 30, "overbought": 70}


def test_recommend_high_vol_uses_tighter_rsi() -> None:
    profile = suggest.recommend(
        _report(MarketRegime.HIGH_VOL),
        symbol="005930",
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert profile.strategy == "rsi_reversion"
    assert profile.params["period"] == 7
    assert profile.weight == 0.5


def test_recommend_trend_down_lowers_weight() -> None:
    profile = suggest.recommend(
        _report(MarketRegime.TREND_DOWN),
        symbol="005930",
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert profile.strategy == "sma_crossover"
    assert profile.weight == 0.3


def test_recommend_is_deterministic_given_now() -> None:
    a = suggest.recommend(
        _report(MarketRegime.RANGE),
        symbol="005930",
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    b = suggest.recommend(
        _report(MarketRegime.RANGE),
        symbol="005930",
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert a == b
