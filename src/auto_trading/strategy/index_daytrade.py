"""IndexMomentumDayTrade — 다회 진입 가능한 day-trade 전략.

진입 조건 (모두 충족 시 BUY 1건):
1. 보유 중 아님 (state 의 last_entry/last_exit 비교).
2. 청산 후 쿨다운(`cooldown_min`) 경과.
3. (선택) 지수 일중 모멘텀 게이트 — `momentum_threshold_pct > 0` 일 때만:
   주입된 `IndexMomentumTracker` 의 `momentum(market, lookback_min)` 결과의
   `lookback_return_pct ≥ threshold` 면 통과.

전략은 외부 호출(REST/WS) 안 함 — tracker 는 인메모리 시계열 조회만 (부작용 X).
tracker 를 1분마다 갱신하는 task 는 호출자(daytrade.start_momentum_loop)가 책임.

청산 신호는 본 전략이 생성하지 않는다 — `risk.PositionGuard` (Phase C) 의 stop/trail/
partial 또는 러너의 `liquidate_at` (15:15) 가 처리. 러너가 SELL 체결 후 본 전략의
`state["last_exit_ts"]` 를 갱신해 다음 진입 조건을 평가한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .._common import Bar, Side
from ..market.index import IndexMomentumTracker, Market
from .base import Signal, Strategy
from .registry import register


@register("index_momentum_daytrade")
class IndexMomentumDayTrade(Strategy):
    """다회 진입 + 쿨다운 + 지수 모멘텀 게이트 day-trade 전략.

    파라미터:
    - `weight` (기본 1.0): Signal.weight. orchestrator 가 1/N 로 주입해 균등 분배.
    - `cooldown_min` (기본 30): 청산 후 재진입 전 대기 분. 0 이면 즉시 재진입.
    - `momentum_lookback_min` (기본 5): 지수 모멘텀 lookback 분.
    - `momentum_threshold_pct` (기본 0): % 단위 진입 임계. 0 이면 게이트 비활성.
    - `momentum_tracker` (기본 None): `IndexMomentumTracker`. 활성 시 주입 필요.
    - `market` (기본 None): 게이트 조회용 시장 (`"KOSPI"` 또는 `"KOSDAQ"`).

    상태 (러너가 갱신):
    - `state["last_entry_ts"]`: 마지막 BUY 발사 시각.
    - `state["last_exit_ts"]`: 마지막 완전 청산 시각 (러너가 SELL 체결 후 갱신).

    어떻게 확인: tests/test_strategy_index_daytrade.py 가 진입·쿨다운·게이트 시나리오 검증.
    """

    name = "index_momentum_daytrade"

    def __init__(self, **params: Any) -> None:
        # tracker/market 은 dataclass-fancy 가 아니라 평범한 속성으로 보관.
        # registry.get(...) 는 키워드만 받으므로 동일 인터페이스로 받아 분리.
        self._tracker: IndexMomentumTracker | None = params.pop("momentum_tracker", None)
        self._market: Market | None = params.pop("market", None)
        super().__init__(**params)

    def on_bar(self, bar: Bar) -> list[Signal]:
        last_entry: datetime | None = self.state.get("last_entry_ts")
        last_exit: datetime | None = self.state.get("last_exit_ts")

        if last_entry is None:
            if not self._momentum_gate_pass(bar):
                return []
            return self._fire_buy(bar)

        # 청산 후 상태인지 — last_exit > last_entry. 그렇지 않으면 보유 중으로 간주.
        if last_exit is None or last_exit <= last_entry:
            return []

        cooldown_min = max(0, int(self.params.get("cooldown_min", 30)))
        if cooldown_min > 0:
            elapsed = bar.ts - last_exit
            if elapsed < timedelta(minutes=cooldown_min):
                return []
        if not self._momentum_gate_pass(bar):
            return []
        return self._fire_buy(bar)

    def _momentum_gate_pass(self, bar: Bar) -> bool:
        """지수 모멘텀 게이트 — threshold 0 이면 항상 통과."""
        threshold = float(self.params.get("momentum_threshold_pct", 0.0))
        if threshold <= 0:
            return True
        if self._tracker is None or self._market is None:
            return False  # 활성인데 주입 안 됐으면 보수적으로 차단.
        lookback = max(1, int(self.params.get("momentum_lookback_min", 5)))
        result = self._tracker.momentum(self._market, lookback_min=lookback, now=bar.ts)
        if result is None or result.lookback_return_pct is None:
            return False
        return result.lookback_return_pct >= threshold

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
