"""백테스트 엔진 결정론성 및 지표 검증."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from auto_trading.backtest.engine import Backtester
from auto_trading.strategy import registry
from auto_trading.strategy.base import Bar, Side, Signal, Strategy


class AlwaysBuyOnceThenSell(Strategy):
    """첫 bar 에 매수, 마지막 bar 에 매도하는 결정론 전략."""

    def __init__(self, total_bars: int) -> None:
        super().__init__(total_bars=total_bars)
        self._i = 0

    def on_bar(self, bar: Bar) -> list[Signal]:
        self._i += 1
        if self._i == 1:
            return [Signal(symbol=bar.symbol, side=Side.BUY, weight=1.0)]
        if self._i == self.params["total_bars"] - 1:
            # 마지막 봉 전에 매도 시그널을 걸면, 실제 체결은 마지막 봉 시가
            return [Signal(symbol=bar.symbol, side=Side.SELL, weight=1.0)]
        return []

    def reset(self) -> None:
        super().reset()
        self._i = 0


def _df(prices: list[float]) -> pd.DataFrame:
    rows = []
    for i, p in enumerate(prices):
        rows.append(
            {
                "ts": datetime(2025, 1, 1) + timedelta(days=i),
                "open": p,
                "high": p,
                "low": p,
                "close": p,
                "volume": 100,
            }
        )
    return pd.DataFrame(rows).set_index("ts")


def test_trade_executes_on_next_bar_open() -> None:
    df = _df([100, 110, 120, 130, 140, 150])
    strat = AlwaysBuyOnceThenSell(total_bars=len(df))
    # 슬리피지 0 명시 주입 — "다음 봉 시가 체결" 의도만 검증.
    bt = Backtester(strategy=strat, initial_cash=100_000.0, slippage_bps=0.0)
    result = bt.run("TEST", df)

    # 매수 1건, 매도 1건
    assert len(result.trades) == 2
    buy, sell = result.trades
    assert buy.side is Side.BUY
    assert sell.side is Side.SELL
    # 매수는 "두 번째 봉의 시가"(=110)
    assert buy.price == 110
    # 매도는 "마지막 봉의 시가"(=150)
    assert sell.price == 150


def test_total_return_positive_on_uptrend() -> None:
    df = _df([100, 110, 120, 130, 140, 150])
    strat = AlwaysBuyOnceThenSell(total_bars=len(df))
    bt = Backtester(strategy=strat, initial_cash=1_000_000.0)
    result = bt.run("TEST", df)
    assert result.report.total_return > 0
    # 수수료/세금 차감 후라도 35%대 근접 (150/110 - 1 ≈ 0.3636)
    assert 0.30 < result.report.total_return < 0.40


def test_backtest_is_deterministic() -> None:
    df = _df([100, 98, 102, 97, 105, 110, 108, 112, 115, 120])
    strat1 = registry.get("sma_crossover", short=3, long=5)
    strat2 = registry.get("sma_crossover", short=3, long=5)
    r1 = Backtester(strategy=strat1, initial_cash=500_000.0).run("X", df)
    r2 = Backtester(strategy=strat2, initial_cash=500_000.0).run("X", df)
    assert r1.report.as_dict() == r2.report.as_dict()
    assert len(r1.trades) == len(r2.trades)


def test_no_trade_when_signal_rejected_by_cash() -> None:
    df = _df([1_000_000, 1_100_000])  # 가격이 예수금보다 비쌈
    strat = AlwaysBuyOnceThenSell(total_bars=len(df))
    bt = Backtester(strategy=strat, initial_cash=100.0)
    result = bt.run("TEST", df)
    # 현금 부족으로 체결 불가
    assert result.trades == []


# ---- 2026-06-10 제3자 검토 — SELL weight 비례 + 인스턴스 재실행 격리 ------------


class BuyThenPartialSell(Strategy):
    """첫 bar 매수 후 weight=0.5 부분 매도 — 라이브(risk manager)와 동일 규칙 검증용."""

    def __init__(self, total_bars: int) -> None:
        super().__init__(total_bars=total_bars)
        self._i = 0

    def on_bar(self, bar: Bar) -> list[Signal]:
        self._i += 1
        if self._i == 1:
            return [Signal(symbol=bar.symbol, side=Side.BUY, weight=1.0)]
        if self._i == self.params["total_bars"] - 1:
            return [Signal(symbol=bar.symbol, side=Side.SELL, weight=0.5)]
        return []

    def reset(self) -> None:
        super().reset()
        self._i = 0


def test_sell_weight_is_proportional_like_live() -> None:
    """SELL weight=0.5 → 보유 절반만 매도. 종전엔 weight 무시하고 전량 매도했다."""
    df = _df([100, 100, 100, 100, 100, 100])
    strat = BuyThenPartialSell(total_bars=len(df))
    bt = Backtester(strategy=strat, initial_cash=100_000.0, slippage_bps=0.0)
    result = bt.run("TEST", df)

    buy, sell = result.trades
    assert buy.side is Side.BUY
    assert sell.side is Side.SELL
    # 보유 buy.quantity 의 절반 (ROUND_HALF_UP — 라이브 risk/manager.py 와 동일).
    assert sell.quantity == round(buy.quantity / 2)
    assert 0 < sell.quantity < buy.quantity


def test_rerun_same_instance_is_isolated() -> None:
    """같은 Backtester 인스턴스 재실행 시 1회차 상태(현금·포지션·에쿼티)가 섞이지 않는다."""
    df = _df([100, 110, 120, 130, 140, 150])
    strat = AlwaysBuyOnceThenSell(total_bars=len(df))
    bt = Backtester(strategy=strat, initial_cash=100_000.0)
    r1 = bt.run("TEST", df)
    r2 = bt.run("TEST", df)
    assert r1.report.as_dict() == r2.report.as_dict()
    assert len(r1.equity_curve) == len(r2.equity_curve)
    assert len(r2.trades) == 2  # 누적 안 됨 (종전엔 1회차 trades 가 합산)
