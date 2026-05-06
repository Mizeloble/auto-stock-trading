"""IndexMomentumDayTrade — 다회 진입 가능한 day-trade 전략 (Phase D 기본).

Phase D 시점 진입 조건:
1. 첫 호출이면(`last_entry_ts` 없음) BUY.
2. 청산 후 쿨다운 경과 (`last_exit_ts > last_entry_ts` AND now - last_exit_ts ≥ cooldown_min) BUY.
3. 그 외(보유 중 또는 쿨다운 안)는 NONE.

청산 신호는 본 전략이 생성하지 않는다 — `risk.PositionGuard` (Phase C) 의 stop/trail/
partial 또는 러너의 `liquidate_at` (15:15) 가 처리. 러너가 SELL 체결 후 본 전략의
`state["last_exit_ts"]` 를 갱신해 다음 진입 조건을 평가한다.

Phase F 에서 지수 일중 모멘텀 게이트가 합쳐지면 진입 조건에 "지수 모멘텀 ≥ threshold"
조건이 추가된다(strategy 가 직접 REST 호출하지 않도록 러너가 `Bar.meta` 또는 별도
컨텍스트로 모멘텀 값을 주입할 예정).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .._common import Bar, Side
from .base import Signal, Strategy
from .registry import register


@register("index_momentum_daytrade")
class IndexMomentumDayTrade(Strategy):
    """다회 진입 가능 day-trade 전략. 청산 후 쿨다운 경과 시 재진입.

    파라미터:
    - `weight` (기본 1.0): Signal.weight. orchestrator 가 1/N 로 주입해 균등 분배.
    - `cooldown_min` (기본 30): 청산 후 재진입 전 대기 분. 0 이면 즉시 재진입.

    상태 (러너가 갱신):
    - `state["last_entry_ts"]`: 마지막 BUY 발사 시각 (전략 자체 갱신).
    - `state["last_exit_ts"]`: 마지막 완전 청산 시각 (러너가 PositionGuard·liquidate
      체결 후 갱신).

    어떻게 확인: tests/test_strategy_index_daytrade.py 가 첫 진입·쿨다운 차단·재진입
    시나리오 검증.
    """

    name = "index_momentum_daytrade"

    def on_bar(self, bar: Bar) -> list[Signal]:
        last_entry: datetime | None = self.state.get("last_entry_ts")
        last_exit: datetime | None = self.state.get("last_exit_ts")

        if last_entry is None:
            return self._fire_buy(bar)

        # 청산 후 상태인지 — last_exit > last_entry. 그렇지 않으면 보유 중으로 간주.
        if last_exit is None or last_exit <= last_entry:
            return []

        cooldown_min = max(0, int(self.params.get("cooldown_min", 30)))
        if cooldown_min > 0:
            elapsed = bar.ts - last_exit
            if elapsed < timedelta(minutes=cooldown_min):
                return []
        return self._fire_buy(bar)

    def _fire_buy(self, bar: Bar) -> list[Signal]:
        weight = float(self.params.get("weight", 1.0))
        weight = max(0.0, min(1.0, weight))
        self.state["last_entry_ts"] = bar.ts
        return [
            Signal(
                symbol=bar.symbol,
                side=Side.BUY,
                weight=weight,
                reason="index_momentum_daytrade_entry",
                ts=bar.ts,
            )
        ]


__all__ = ["IndexMomentumDayTrade"]
