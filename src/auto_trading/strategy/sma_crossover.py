"""이동평균선 골든/데드크로스 전략.

- 단기 이평(기본 5일)이 장기 이평(기본 20일)을 상향 돌파 → 매수 Signal
- 단기 이평이 장기 이평을 하향 돌파 → 매도 Signal
- 그 외엔 시그널 없음

이 전략은 "추세추종"의 가장 기본 형태이며, 횡보장에선 휩소(톱니)에 당할 수 있다.
초보 학습 & 프레임워크 동작 확인용.
"""

from __future__ import annotations

from collections import deque

from . import indicators as ind
from .base import Bar, Side, Signal, Strategy
from .registry import register


@register("sma_crossover")
class SmaCrossover(Strategy):
    def __init__(self, short: int = 5, long: int = 20) -> None:
        if short <= 0 or long <= 0 or short >= long:
            raise ValueError("require 0 < short < long")
        super().__init__(short=short, long=long)
        self._prices: deque[float] = deque(maxlen=long)
        self._prev_diff: float | None = None

    def reset(self) -> None:
        super().reset()
        self._prices.clear()
        self._prev_diff = None

    def on_bar(self, bar: Bar) -> list[Signal]:
        short: int = self.params["short"]
        long: int = self.params["long"]

        self._prices.append(bar.close)
        if len(self._prices) < long:
            return []

        prices = list(self._prices)
        short_ma = ind.sma(prices, short)
        long_ma = ind.sma(prices, long)
        assert short_ma is not None and long_ma is not None  # len 체크 통과 후 보장
        diff = short_ma - long_ma

        signals: list[Signal] = []
        if self._prev_diff is not None:
            if self._prev_diff <= 0 < diff:
                signals.append(
                    Signal(
                        symbol=bar.symbol,
                        side=Side.BUY,
                        weight=1.0,
                        reason=f"golden cross (short={short_ma:.2f} > long={long_ma:.2f})",
                        ts=bar.ts,
                    )
                )
            elif self._prev_diff >= 0 > diff:
                signals.append(
                    Signal(
                        symbol=bar.symbol,
                        side=Side.SELL,
                        weight=1.0,
                        reason=f"dead cross (short={short_ma:.2f} < long={long_ma:.2f})",
                        ts=bar.ts,
                    )
                )

        self._prev_diff = diff
        return signals
