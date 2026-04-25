"""RSI 역추세 전략 검증 (결정론, 네트워크 없음)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from auto_trading._common import Bar, Side
from auto_trading.strategy import registry
from auto_trading.strategy.rsi_reversion import RsiReversion


def _bar(close: float, idx: int = 0) -> Bar:
    return Bar(
        symbol="TEST",
        ts=datetime(2025, 1, 2, 9, 0, tzinfo=UTC) + timedelta(days=idx),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1,
    )


def test_warmup_period_returns_no_signal() -> None:
    strat = RsiReversion(period=14)
    # period+1 개 미만이면 시그널 없음
    for i in range(5):
        assert strat.on_bar(_bar(100 + i, i)) == []


def test_rebound_from_oversold_emits_buy() -> None:
    """과매도 → 상승 반등 시 BUY 시그널."""
    strat = RsiReversion(period=4, oversold=30.0, overbought=70.0)

    # 14일 하락 (RSI 극저)
    prices = [100, 98, 95, 90, 85, 80, 75, 70, 65, 60, 55, 50, 45, 42]
    for i, p in enumerate(prices):
        strat.on_bar(_bar(p, i))

    # 이제 강한 반등 → RSI 가 neutral 로 복귀해야 함
    bounce = [60, 70, 80]
    signals: list = []
    for j, p in enumerate(bounce):
        signals.extend(strat.on_bar(_bar(p, len(prices) + j)))

    buys = [s for s in signals if s.side is Side.BUY]
    assert buys, "반등 이후 BUY 시그널이 나와야 합니다"


def test_pullback_from_overbought_emits_sell() -> None:
    strat = RsiReversion(period=4, oversold=30.0, overbought=70.0)

    # 연속 상승 → RSI 극고
    prices = [100, 102, 105, 108, 112, 115, 118, 120, 125, 128, 132, 135, 138, 140]
    for i, p in enumerate(prices):
        strat.on_bar(_bar(p, i))

    # 하락 전환 → overbought 에서 neutral 로
    pullback = [130, 120, 110]
    signals: list = []
    for j, p in enumerate(pullback):
        signals.extend(strat.on_bar(_bar(p, len(prices) + j)))

    sells = [s for s in signals if s.side is Side.SELL]
    assert sells, "하락 전환 시 SELL 시그널이 나와야 합니다"


def test_registry_exposes_rsi() -> None:
    assert "rsi_reversion" in registry.available()
    strat = registry.get("rsi_reversion", period=10)
    assert isinstance(strat, RsiReversion)
    assert strat.params["period"] == 10


def test_reset_clears_internal_state() -> None:
    strat = RsiReversion(period=4)
    for i, p in enumerate([100, 99, 98, 97, 96]):
        strat.on_bar(_bar(p, i))
    strat.reset()
    assert strat._avg_gain is None
    assert strat._avg_loss is None
    assert strat._prev_zone is None
    assert len(strat._closes) == 0
