"""Backtester 의 atr_normalized sizing 회귀.

29단계 PR21 (#6) — 변동성 큰 종목에 자동으로 작은 수량 들어가는지 검증.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from auto_trading.backtest.engine import Backtester
from auto_trading.strategy.base import Bar, Side, Signal, Strategy


class BuyOnceAtBar(Strategy):
    """지정한 bar 인덱스에 BUY 1회. ATR sizing 테스트 안정성용 — sell 안 함."""

    def __init__(self, trigger_index: int = 30) -> None:
        super().__init__(trigger_index=trigger_index)
        self._i = 0

    def on_bar(self, bar: Bar) -> list[Signal]:
        self._i += 1
        if self._i == self.params["trigger_index"]:
            return [Signal(symbol=bar.symbol, side=Side.BUY, weight=1.0)]
        return []

    def reset(self) -> None:
        super().reset()
        self._i = 0


def _ohlc_df(*, n: int, base_price: float, atr_pct: float) -> pd.DataFrame:
    """n 봉, close 일정, high/low 가 close ± atr_pct 비율인 단순 데이터.

    atr_pct=0.005 면 ATR ≈ base_price * 0.01 (high-low = 2 * atr_pct * close).
    """
    rows = []
    for i in range(n):
        rows.append(
            {
                "ts": datetime(2025, 1, 1) + timedelta(days=i),
                "open": base_price,
                "high": base_price * (1 + atr_pct),
                "low": base_price * (1 - atr_pct),
                "close": base_price,
                "volume": 1_000,
            }
        )
    return pd.DataFrame(rows).set_index("ts")


def test_all_in_sizing_unchanged_by_default() -> None:
    """default position_sizing='all_in' 은 기존 동작 — 가용 현금 전부."""
    bt = Backtester(
        strategy=BuyOnceAtBar(trigger_index=5),
        initial_cash=10_000_000.0,
    )
    df = _ohlc_df(n=10, base_price=10_000.0, atr_pct=0.01)
    result = bt.run("X", df)
    # 첫 매수 = 6번째 봉 시가 (시그널은 5번째 → 6번째 시가 체결).
    buys = [t for t in result.trades if t.side is Side.BUY]
    assert len(buys) == 1
    # ~1,000 주 매수 (10_000_000 / (10000 * 1.00015)) ≈ 999.85
    assert buys[0].quantity >= 990


def test_atr_normalized_smaller_qty_for_higher_volatility() -> None:
    """동일 자본 + 동일 가격 환경에서 ATR 큰 쪽이 더 작은 수량."""
    df_low_vol = _ohlc_df(n=30, base_price=10_000.0, atr_pct=0.005)  # ATR ~100
    df_high_vol = _ohlc_df(n=30, base_price=10_000.0, atr_pct=0.05)  # ATR ~1000

    def _qty(df: pd.DataFrame) -> int:
        bt = Backtester(
            strategy=BuyOnceAtBar(trigger_index=20),
            initial_cash=10_000_000.0,
            position_sizing="atr_normalized",
            atr_period=14,
            atr_risk_pct=0.01,
        )
        result = bt.run("X", df)
        buys = [t for t in result.trades if t.side is Side.BUY]
        return buys[0].quantity if buys else 0

    qty_low = _qty(df_low_vol)
    qty_high = _qty(df_high_vol)

    assert qty_low > 0, "낮은 변동성에서 매수가 일어나야"
    assert qty_high > 0, "높은 변동성에서도 매수는 일어남 (수량만 작아짐)"
    # ATR 가 10배 크면 수량은 약 1/10. 정확한 비율은 cash 한도가 안 걸리는 범위에서.
    assert qty_high < qty_low / 5, (
        f"고변동성 수량이 저변동성의 1/5 미만이어야 — qty_low={qty_low}, qty_high={qty_high}"
    )


def test_atr_normalized_no_position_when_history_insufficient() -> None:
    """ATR period+1 봉 미달이면 진입 X (안전)."""
    bt = Backtester(
        strategy=BuyOnceAtBar(trigger_index=2),  # 너무 빨리 시그널 → ATR 미계산
        initial_cash=10_000_000.0,
        position_sizing="atr_normalized",
        atr_period=14,
    )
    # 5봉만 — atr_period=14 +1 = 15봉 필요.
    df = _ohlc_df(n=5, base_price=10_000.0, atr_pct=0.01)
    result = bt.run("X", df)
    buys = [t for t in result.trades if t.side is Side.BUY]
    # 시그널은 났지만 수량 0 → 체결 0.
    assert buys == []


def test_atr_normalized_respects_cash_limit() -> None:
    """ATR 가 너무 작아 qty_by_risk 가 가용 현금 넘으면 cash 한도로 cap."""
    bt = Backtester(
        strategy=BuyOnceAtBar(trigger_index=20),
        initial_cash=1_000_000.0,  # 적은 자본
        position_sizing="atr_normalized",
        atr_period=14,
        atr_risk_pct=10.0,  # 비현실적으로 큰 위험 — cash 한도 작동 검증.
    )
    df = _ohlc_df(n=30, base_price=10_000.0, atr_pct=0.001)  # ATR 매우 작음
    result = bt.run("X", df)
    buys = [t for t in result.trades if t.side is Side.BUY]
    assert len(buys) == 1
    # 1_000_000 자본 + 가격 10_000 + commission → 약 99주 한도.
    assert buys[0].quantity <= 100


def test_atr_normalized_with_weight_scales_position() -> None:
    """signal.weight 가 atr_normalized 에서도 곱셈 인자로 작동."""

    class _BuyWithWeight(Strategy):
        def __init__(self, weight: float, trigger: int) -> None:
            super().__init__(weight=weight, trigger=trigger)
            self._i = 0

        def on_bar(self, bar: Bar) -> list[Signal]:
            self._i += 1
            if self._i == self.params["trigger"]:
                return [Signal(symbol=bar.symbol, side=Side.BUY, weight=self.params["weight"])]
            return []

        def reset(self) -> None:
            super().reset()
            self._i = 0

    df = _ohlc_df(n=30, base_price=10_000.0, atr_pct=0.01)

    def _qty(weight: float) -> int:
        bt = Backtester(
            strategy=_BuyWithWeight(weight=weight, trigger=20),
            initial_cash=10_000_000.0,
            position_sizing="atr_normalized",
            atr_period=14,
            atr_risk_pct=0.01,
        )
        result = bt.run("X", df)
        buys = [t for t in result.trades if t.side is Side.BUY]
        return buys[0].quantity if buys else 0

    qty_full = _qty(1.0)
    qty_half = _qty(0.5)
    assert qty_half > 0
    # 절반 weight = 절반 수량 (정수 절삭으로 +-1 오차).
    assert abs(qty_half - qty_full // 2) <= 1
