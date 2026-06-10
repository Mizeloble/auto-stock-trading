"""_round_trips 매수 수수료 안분 회귀 (2026-06-10 제3자 검토).

종전 버그: 매수 lot 전체 수수료를 *매도 수량* 분모로 안분 —
- 피라미딩(2회 매수 → 1회 전량 매도): 매수 수수료 과소 차감 → 가짜 승리.
- 부분 매도(한 lot 을 여러 매도가 소진): lot 수수료를 매 매도마다 재차감 → 가짜 패배.
수정: lot 에 주당 수수료(fee/quantity)를 저장해 matched 수량만큼만 차감.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from auto_trading.backtest.broker import Trade
from auto_trading.backtest.metrics import _round_trips
from auto_trading.strategy.base import Side

_TS = datetime(2026, 6, 1)


def _trade(side: Side, price: float, qty: int, fee: float = 0.0, tax: float = 0.0) -> Trade:
    return Trade(symbol="X", side=side, ts=_TS, price=price, quantity=qty, fee=fee, tax=tax)


def test_pyramiding_buy_fees_fully_deducted() -> None:
    """100주(fee 15) + 50주(fee 7.5) 매수 → 150주 전량 매도: 각 lot 수수료 전액 차감."""
    pnls = _round_trips(
        [
            _trade(Side.BUY, 100.0, 100, fee=15.0),
            _trade(Side.BUY, 100.0, 50, fee=7.5),
            _trade(Side.SELL, 110.0, 150),
        ]
    )
    # lot1: (110-100)*100 - 15 = 985 / lot2: (110-100)*50 - 7.5 = 492.5
    # (종전엔 15*(100/150)=10, 7.5*(50/150)=2.5 만 차감돼 990/497.5 로 부풀려짐)
    assert pnls == pytest.approx([985.0, 492.5])


def test_partial_sells_do_not_double_count_buy_fee() -> None:
    """한 lot(fee 15)을 두 번에 나눠 매도해도 매수 수수료는 총 1회만 차감."""
    pnls = _round_trips(
        [
            _trade(Side.BUY, 100.0, 100, fee=15.0),
            _trade(Side.SELL, 110.0, 50),
            _trade(Side.SELL, 110.0, 50),
        ]
    )
    # 각각 (110-100)*50 - 0.15*50 = 492.5 — 합산 차감액 = 15 (종전엔 15+15=30).
    assert pnls == pytest.approx([492.5, 492.5])
    assert sum(pnls) == pytest.approx(1000.0 - 15.0)


def test_sell_costs_prorated_across_lots() -> None:
    """매도 수수료·세금은 매도 trade 기준 안분 — 기존 동작 보존."""
    pnls = _round_trips(
        [
            _trade(Side.BUY, 100.0, 100),
            _trade(Side.BUY, 100.0, 100),
            _trade(Side.SELL, 110.0, 200, fee=20.0, tax=40.0),
        ]
    )
    # 각 lot: (110-100)*100 - 60*(100/200) = 1000 - 30 = 970
    assert pnls == pytest.approx([970.0, 970.0])
