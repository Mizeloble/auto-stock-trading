"""day-trade 진입 전략 — IntradayMomentum.

발굴기(`market.rank.top_volume`) 가 골라낸 종목에 대해 첫 봉이 들어오는 순간
시장가 BUY 시그널 한 번만 발사하고 끝. 이후 봉은 NONE.

청산은 러너의 `liquidate_at` 시계 task 가 별도로 처리하므로 전략 자체는 매도 시그널 없음.
"""

from __future__ import annotations

from .._common import Bar, Side
from .base import Signal, Strategy
from .registry import register


@register("intraday_momentum")
class IntradayMomentum(Strategy):
    """단일 진입 — 첫 봉에 weight=`weight` 로 BUY, 이후 NONE.

    파라미터:
    - `weight` (기본 1.0): Signal.weight. orchestrator 가 1/N 으로 주입해 균등 분배.

    상태:
    - `state["fired"]` : True 면 더 이상 시그널 안 냄.
    """

    name = "intraday_momentum"

    def on_bar(self, bar: Bar) -> list[Signal]:
        if self.state.get("fired"):
            return []
        weight = float(self.params.get("weight", 1.0))
        weight = max(0.0, min(1.0, weight))
        self.state["fired"] = True
        return [
            Signal(
                symbol=bar.symbol,
                side=Side.BUY,
                weight=weight,
                reason="day_open_momentum",
                ts=bar.ts,
            )
        ]


__all__ = ["IntradayMomentum"]
