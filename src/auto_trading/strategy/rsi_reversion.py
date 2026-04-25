"""RSI 역추세 전략 (Relative Strength Index mean-reversion).

- RSI 계산: Wilder 의 지수평활 근사 (period 기본 14).
- 과매도(RSI ≤ oversold, 기본 30) → 매수 Signal.
- 과매수(RSI ≥ overbought, 기본 70) → 매도 Signal.
- 경계를 "다시 벗어날 때" 만 시그널을 내보내 연속 진입을 막는다.

주의: 강한 추세장에서는 반대로 물릴 수 있어 추세 확인 필터와 함께 쓰는 게 보통이다.
이 파일은 플러그인 구조 검증 + 초보 학습용.
"""

from __future__ import annotations

from collections import deque

from .base import Bar, Side, Signal, Strategy
from .registry import register


@register("rsi_reversion")
class RsiReversion(Strategy):
    def __init__(
        self,
        period: int = 14,
        oversold: float = 30.0,
        overbought: float = 70.0,
    ) -> None:
        if period <= 1:
            raise ValueError("period 는 2 이상")
        if not 0 < oversold < overbought < 100:
            raise ValueError("0 < oversold < overbought < 100 여야 합니다")
        super().__init__(period=period, oversold=oversold, overbought=overbought)
        self._closes: deque[float] = deque(maxlen=period + 1)
        self._avg_gain: float | None = None
        self._avg_loss: float | None = None
        # 직전 봉의 "구역": 'oversold' / 'overbought' / 'neutral' — 경계 재진입 시점 감지.
        self._prev_zone: str | None = None

    def reset(self) -> None:
        super().reset()
        self._closes.clear()
        self._avg_gain = None
        self._avg_loss = None
        self._prev_zone = None

    def on_bar(self, bar: Bar) -> list[Signal]:
        period: int = self.params["period"]
        oversold: float = self.params["oversold"]
        overbought: float = self.params["overbought"]

        self._closes.append(bar.close)
        if len(self._closes) < period + 1:
            return []

        if self._avg_gain is None or self._avg_loss is None:
            # 최초 계산: 단순평균으로 초기화
            gains, losses = 0.0, 0.0
            closes = list(self._closes)
            for i in range(1, len(closes)):
                delta = closes[i] - closes[i - 1]
                if delta >= 0:
                    gains += delta
                else:
                    losses += -delta
            self._avg_gain = gains / period
            self._avg_loss = losses / period
        else:
            delta = self._closes[-1] - self._closes[-2]
            gain = delta if delta > 0 else 0.0
            loss = -delta if delta < 0 else 0.0
            self._avg_gain = (self._avg_gain * (period - 1) + gain) / period
            self._avg_loss = (self._avg_loss * (period - 1) + loss) / period

        if self._avg_loss == 0:
            rsi = 100.0
        else:
            rs = self._avg_gain / self._avg_loss
            rsi = 100.0 - (100.0 / (1.0 + rs))

        zone = "oversold" if rsi <= oversold else "overbought" if rsi >= overbought else "neutral"
        signals: list[Signal] = []
        if self._prev_zone == "oversold" and zone == "neutral":
            signals.append(
                Signal(
                    symbol=bar.symbol,
                    side=Side.BUY,
                    weight=1.0,
                    reason=f"RSI {rsi:.1f} rebound from oversold",
                    ts=bar.ts,
                )
            )
        elif self._prev_zone == "overbought" and zone == "neutral":
            signals.append(
                Signal(
                    symbol=bar.symbol,
                    side=Side.SELL,
                    weight=1.0,
                    reason=f"RSI {rsi:.1f} pullback from overbought",
                    ts=bar.ts,
                )
            )

        self._prev_zone = zone
        return signals
