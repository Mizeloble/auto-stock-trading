"""SMA 크로스오버 전략 시그널 검증."""

from __future__ import annotations

from datetime import datetime, timedelta

from auto_trading.strategy import registry
from auto_trading.strategy.base import Bar, Side


def _bar(price: float, i: int, symbol: str = "TEST") -> Bar:
    return Bar(
        symbol=symbol,
        ts=datetime(2025, 1, 1) + timedelta(days=i),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=1,
    )


def test_golden_and_dead_cross_emits_signals() -> None:
    strat = registry.get("sma_crossover", short=3, long=5)

    # 하락 → 상승 → 하락 패턴. 3/5 이평이 충분히 축적된 뒤 교차.
    prices = [
        10,
        9,
        8,
        7,
        6,  # long=5 채워짐, short ma < long ma (음수 diff)
        8,
        10,
        12,
        14,
        16,  # 상승 추세 → golden cross 발생
        14,
        12,
        10,
        8,
        6,
    ]  # 하락 → dead cross 발생

    signals = []
    for i, p in enumerate(prices):
        signals.extend(strat.on_bar(_bar(p, i)))

    sides = [s.side for s in signals]
    assert Side.BUY in sides
    assert Side.SELL in sides
    # 매수가 매도보다 먼저 나와야 함
    buy_idx = next(i for i, s in enumerate(signals) if s.side is Side.BUY)
    sell_idx = next(i for i, s in enumerate(signals) if s.side is Side.SELL)
    assert buy_idx < sell_idx


def test_no_signal_before_warmup() -> None:
    strat = registry.get("sma_crossover", short=3, long=5)
    # long=5 인데 4개만 입력 → 시그널 없음
    out = []
    for i, p in enumerate([10, 9, 11, 12]):
        out.extend(strat.on_bar(_bar(p, i)))
    assert out == []


def test_registry_lists_builtin() -> None:
    names = registry.available()
    assert "sma_crossover" in names


def test_reset_clears_state() -> None:
    strat = registry.get("sma_crossover", short=2, long=3)
    for i, p in enumerate([1, 2, 3]):
        strat.on_bar(_bar(p, i))
    strat.reset()
    assert strat._prev_diff is None
    assert list(strat._prices) == []
