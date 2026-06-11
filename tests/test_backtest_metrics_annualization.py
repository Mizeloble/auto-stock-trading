"""연환산 계수 추정 회귀 (2026-06-10 제3자 검토).

종전엔 x252 하드코딩 — 1분봉 equity curve 를 넣으면 Sharpe·연변동성이
sqrt(390)≈19.7배 부풀려졌다. 봉 간격 중앙값으로 일봉=252, 분봉=252 x 하루 봉수.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from auto_trading.backtest.metrics import _periods_per_year, compute


def _curve(start: datetime, step: timedelta, n: int, *, seed: int = 7) -> pd.Series:
    rng = np.random.default_rng(seed)
    returns = rng.normal(loc=0.0001, scale=0.001, size=n)
    values = 1_000_000 * np.cumprod(1 + returns)
    index = pd.DatetimeIndex([start + step * i for i in range(n)], name="ts")
    return pd.Series(values, index=index, name="equity")


def test_daily_bars_keep_252() -> None:
    curve = _curve(datetime(2025, 1, 1), timedelta(days=1), 100)
    assert _periods_per_year(curve.index) == pytest.approx(252.0)


def test_minute_bars_scale_by_bars_per_day() -> None:
    curve = _curve(datetime(2025, 1, 2, 9, 0), timedelta(minutes=1), 390)
    # 1분봉 — 하루 390봉 x 252일
    assert _periods_per_year(curve.index) == pytest.approx(252.0 * 390)


def test_minute_curve_uses_correct_annualization_factor() -> None:
    """같은 per-bar 수익률 시퀀스 — Sharpe = (mean/std)·sqrt(연간 봉수) 검증.

    종전(x252 고정)에는 분봉도 일봉과 같은 sqrt(252) 가 적용돼, 분봉/일봉
    Sharpe 비율이 1 로 나왔다 (라벨은 '연환산'인데 실제로는 비교 불능 숫자).
    올바른 연환산에서는 비율이 sqrt(연간 분봉수/연간 일봉수)=sqrt(390) 이다.
    """
    daily = _curve(datetime(2025, 1, 1), timedelta(days=1), 500, seed=11)
    minute = _curve(datetime(2025, 1, 2, 9, 0), timedelta(minutes=1), 500, seed=11)
    r_daily = compute(daily, trades=[], initial_equity=1_000_000)
    r_minute = compute(minute, trades=[], initial_equity=1_000_000)
    assert r_minute.sharpe / r_daily.sharpe == pytest.approx(390.0**0.5, rel=1e-6)


def test_overnight_gaps_do_not_skew_minute_estimate() -> None:
    """이틀치 1분봉 사이의 야간 갭(17.5h)이 끼어도 중앙값 추정은 1분 간격."""
    day1 = [datetime(2025, 1, 2, 9, 0) + timedelta(minutes=i) for i in range(390)]
    day2 = [datetime(2025, 1, 3, 9, 0) + timedelta(minutes=i) for i in range(390)]
    index = pd.DatetimeIndex(day1 + day2)
    assert _periods_per_year(index) == pytest.approx(252.0 * 390)


def test_short_index_falls_back_to_daily() -> None:
    index = pd.DatetimeIndex([datetime(2025, 1, 1), datetime(2025, 1, 2)])
    assert _periods_per_year(index) == pytest.approx(252.0)
