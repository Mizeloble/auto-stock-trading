"""인디케이터 헬퍼 단위테스트 — 알려진 시리즈로 수치 회귀."""

from __future__ import annotations

import math

import pytest

from auto_trading.strategy import indicators as ind


def test_sma_basic() -> None:
    assert ind.sma([1.0, 2.0, 3.0, 4.0, 5.0], 5) == pytest.approx(3.0)
    assert ind.sma([10, 20, 30], 2) == pytest.approx(25.0)


def test_sma_undersized_returns_none() -> None:
    assert ind.sma([1, 2], 5) is None


def test_sma_invalid_period_raises() -> None:
    with pytest.raises(ValueError):
        ind.sma([1, 2, 3], 0)


def test_ema_matches_seed() -> None:
    # 첫 EMA 는 첫 period 의 단순평균과 같아야 한다 (그 이후 봉이 없을 때).
    assert ind.ema([2.0, 4.0, 6.0], 3) == pytest.approx(4.0)


def test_ema_progression() -> None:
    # period=2: seed=(1+3)/2=2, 다음봉=5 → 5*2/3 + 2*1/3 = 4.0
    assert ind.ema([1.0, 3.0, 5.0], 2) == pytest.approx(4.0)


def test_rsi_wilder_constant_series_returns_100() -> None:
    # 손실이 0이면 RSI=100
    rsi = ind.rsi_wilder([10.0] * 20, period=14)
    assert rsi == 100.0


def test_rsi_wilder_monotone_up_high() -> None:
    closes = [float(x) for x in range(1, 30)]
    rsi = ind.rsi_wilder(closes, period=14)
    assert rsi == 100.0  # 손실 0


def test_rsi_wilder_step_matches_full_compute() -> None:
    closes = [10.0, 11.0, 10.5, 12.0, 11.0, 13.0, 12.0, 14.0, 13.0, 15.0, 14.0, 16.0, 15.0]
    period = 4
    full = ind.rsi_wilder(closes, period)
    seed = ind.rsi_wilder_seed(closes[: period + 1], period)
    assert seed is not None
    avg_gain, avg_loss = seed
    rsi = None
    for i in range(period + 1, len(closes)):
        avg_gain, avg_loss, rsi = ind.rsi_wilder_step(
            avg_gain, avg_loss, closes[i] - closes[i - 1], period
        )
    assert rsi is not None and full is not None
    assert math.isclose(rsi, full, abs_tol=1e-9)


def test_atr_known_values() -> None:
    highs = [11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0]
    lows = [9.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0]
    closes = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0]
    val = ind.atr(highs, lows, closes, period=3)
    # TR 시리즈: max(2, |11-10|, |9-10|)=2, ... 계속 2 → ATR=2
    assert val == pytest.approx(2.0)


def test_bollinger_centers_on_mean() -> None:
    closes = [10.0, 12.0, 11.0, 13.0, 14.0]
    bb = ind.bollinger(closes, period=5, k=2.0)
    assert bb is not None
    mid, upper, lower = bb
    assert mid == pytest.approx(12.0)
    assert upper > mid > lower
