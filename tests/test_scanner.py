"""시장국면 스캐너 분류 회귀."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from auto_trading.strategy import scanner
from auto_trading.strategy.scanner import MarketRegime


def _make_df(closes: list[float], *, atr_factor: float = 0.005) -> pd.DataFrame:
    """OHLCV DataFrame 합성. high/low 는 close 대비 atr_factor 만큼 띄움."""
    rows = []
    base = datetime(2025, 1, 1)
    for i, c in enumerate(closes):
        rng = c * atr_factor
        rows.append(
            {
                "ts": base + timedelta(days=i),
                "open": c,
                "high": c + rng,
                "low": c - rng,
                "close": c,
                "volume": 1,
            }
        )
    return pd.DataFrame(rows).set_index("ts")


def test_classify_trend_up() -> None:
    closes = [100.0 + i * 1.0 for i in range(60)]  # 매일 +1, 평균 130 → +0.77%/봉
    df = _make_df(closes, atr_factor=0.003)
    report = scanner.classify(df)
    assert report.regime == MarketRegime.TREND_UP
    assert report.metrics["slope_pct"] > scanner.SLOPE_TREND_THRESHOLD


def test_classify_trend_down() -> None:
    closes = [200.0 - i * 1.0 for i in range(60)]
    df = _make_df(closes, atr_factor=0.003)
    report = scanner.classify(df)
    assert report.regime == MarketRegime.TREND_DOWN
    assert report.metrics["slope_pct"] < -scanner.SLOPE_TREND_THRESHOLD


def test_classify_range() -> None:
    # 100±0.5 박스 — 기울기 거의 0, 변동성 낮음
    closes = [100.0 + (1 if i % 2 else -1) * 0.5 for i in range(60)]
    df = _make_df(closes, atr_factor=0.002)
    report = scanner.classify(df)
    assert report.regime == MarketRegime.RANGE


def test_classify_high_vol() -> None:
    # 큰 진폭 (ATR ~ 5% of close) 으로 HIGH_VOL 분기 강제
    closes = [100.0 + (i % 5) * 0.1 for i in range(60)]
    df = _make_df(closes, atr_factor=0.05)
    report = scanner.classify(df)
    assert report.regime == MarketRegime.HIGH_VOL


def test_classify_insufficient_data_falls_back_to_range() -> None:
    df = _make_df([100.0, 101.0, 99.0])
    report = scanner.classify(df)
    assert report.regime == MarketRegime.RANGE
    assert "데이터 부족" in report.summary_ko
