"""인디케이터 헬퍼 (순수 함수).

전략·시장국면 분석기·LLM 어드바이저가 공통으로 사용한다.
입력은 `Sequence[float]` 만 받아 부작용·시계 의존이 없다.
"""

from __future__ import annotations

from collections.abc import Sequence


def sma(values: Sequence[float], period: int) -> float | None:
    """단순이동평균. period 미달이면 None."""
    if period <= 0:
        raise ValueError("period must be > 0")
    if len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / period


def ema(values: Sequence[float], period: int) -> float | None:
    """지수이동평균. 첫 EMA 는 첫 period 의 SMA 로 시드."""
    if period <= 0:
        raise ValueError("period must be > 0")
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    e = seed
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def rsi_wilder_seed(closes: Sequence[float], period: int) -> tuple[float, float] | None:
    """Wilder RSI 초기값. 첫 period+1 봉의 단순평균으로 (avg_gain, avg_loss) 반환.

    period+1 봉 미달이면 None.
    """
    if period <= 1:
        raise ValueError("period must be > 1")
    if len(closes) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses += -delta
    return gains / period, losses / period


def rsi_wilder(closes: Sequence[float], period: int) -> float | None:
    """Wilder RSI 풀계산. 워밍업(period+1봉) 미달이면 None."""
    seed = rsi_wilder_seed(closes, period)
    if seed is None:
        return None
    avg_gain, avg_loss = seed
    for i in range(period + 1, len(closes)):
        delta = closes[i] - closes[i - 1]
        avg_gain, avg_loss, _ = rsi_wilder_step(avg_gain, avg_loss, delta, period)
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def rsi_wilder_step(
    prev_avg_gain: float,
    prev_avg_loss: float,
    delta: float,
    period: int,
) -> tuple[float, float, float]:
    """이전 EMA 게인/로스에 새 변동을 1봉 반영해 (avg_gain, avg_loss, rsi) 반환.

    스트리밍 갱신용 — 전체 시리즈를 다시 돌리지 않는다.
    """
    if period <= 1:
        raise ValueError("period must be > 1")
    gain = delta if delta > 0 else 0.0
    loss = -delta if delta < 0 else 0.0
    avg_gain = (prev_avg_gain * (period - 1) + gain) / period
    avg_loss = (prev_avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        rsi = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))
    return avg_gain, avg_loss, rsi


def true_range(high: float, low: float, prev_close: float) -> float:
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int,
) -> float | None:
    """평균 진폭 (Wilder ATR). period+1 봉 미달이면 None."""
    if period <= 0:
        raise ValueError("period must be > 0")
    n = len(closes)
    if n != len(highs) or n != len(lows):
        raise ValueError("highs/lows/closes 길이가 같아야 합니다")
    if n < period + 1:
        return None
    trs = [true_range(highs[i], lows[i], closes[i - 1]) for i in range(1, n)]
    seed = sum(trs[:period]) / period
    a = seed
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a


def bollinger(
    closes: Sequence[float],
    period: int,
    k: float = 2.0,
) -> tuple[float, float, float] | None:
    """(mid, upper, lower). period 미달이면 None."""
    if period <= 1:
        raise ValueError("period must be > 1")
    if len(closes) < period:
        return None
    window = list(closes[-period:])
    mid = sum(window) / period
    var = sum((x - mid) ** 2 for x in window) / period
    sd = var**0.5
    return mid, mid + k * sd, mid - k * sd


__all__ = [
    "atr",
    "bollinger",
    "ema",
    "rsi_wilder",
    "rsi_wilder_seed",
    "rsi_wilder_step",
    "sma",
    "true_range",
]
