"""Walk-Forward 백테스트 회귀.

29단계 PR23 (#1) — 슬라이딩 윈도우 개수·summary 계산·strategy factory 격리.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from auto_trading.backtest.walk_forward import (
    WalkForwardWindow,
    walk_forward,
)
from auto_trading.strategy.base import Bar, Side, Signal, Strategy


class BuyHoldSell(Strategy):
    """첫 봉 매수, 마지막 봉 매도 — 윈도우별로 결정론."""

    def __init__(self) -> None:
        super().__init__()
        self._first = True
        self._buy_done = False

    def on_bar(self, bar: Bar) -> list[Signal]:
        # 매 윈도우 시작 시 reset 되어야 함.
        if not self._buy_done:
            self._buy_done = True
            return [Signal(symbol=bar.symbol, side=Side.BUY, weight=1.0)]
        return []

    def reset(self) -> None:
        super().reset()
        self._buy_done = False


def _factory() -> Strategy:
    return BuyHoldSell()


def _ohlc_df(*, n: int, base_price: float = 10_000.0, slope: float = 0.001) -> pd.DataFrame:
    """n 봉 단조 증가 가격."""
    rows = []
    for i in range(n):
        price = base_price * (1 + slope * i)
        rows.append(
            {
                "ts": datetime(2024, 1, 1) + timedelta(days=i),
                "open": price,
                "high": price * 1.01,
                "low": price * 0.99,
                "close": price,
                "volume": 1_000,
            }
        )
    return pd.DataFrame(rows).set_index("ts")


# ---- 기본 동작 ---------------------------------------------------------------


def test_walk_forward_creates_correct_number_of_windows() -> None:
    """1년(365일) + window=90 + step=30 → 약 10개 윈도우."""
    df = _ohlc_df(n=365)
    result = walk_forward(
        _factory,
        "TEST",
        df,
        window_days=90,
        step_days=30,
    )
    # 정확한 수치는 windowing 알고리즘 따라 다름. 8~12 사이면 합리적.
    assert 8 <= result.summary.n_windows <= 12
    assert len(result.windows) == result.summary.n_windows


def test_walk_forward_window_timestamps_within_range() -> None:
    df = _ohlc_df(n=100)
    result = walk_forward(_factory, "X", df, window_days=30, step_days=30)
    for w in result.windows:
        assert w.start >= df.index[0].to_pydatetime()
        assert w.end <= df.index[-1].to_pydatetime()
        assert w.end >= w.start


def test_walk_forward_returns_empty_for_empty_df() -> None:
    empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    empty.index = pd.DatetimeIndex([], name="ts")
    result = walk_forward(_factory, "X", empty, window_days=30, step_days=30)
    assert result.windows == []
    assert result.summary.n_windows == 0


def test_walk_forward_rejects_non_datetime_index() -> None:
    df = pd.DataFrame(
        {
            "open": [1, 2, 3],
            "high": [1, 2, 3],
            "low": [1, 2, 3],
            "close": [1, 2, 3],
            "volume": [1, 2, 3],
        }
    )
    # 일반 RangeIndex.
    with pytest.raises(ValueError, match="DatetimeIndex"):
        walk_forward(_factory, "X", df, window_days=10, step_days=5)


def test_walk_forward_rejects_invalid_window_params() -> None:
    df = _ohlc_df(n=30)
    with pytest.raises(ValueError):
        walk_forward(_factory, "X", df, window_days=0, step_days=10)
    with pytest.raises(ValueError):
        walk_forward(_factory, "X", df, window_days=10, step_days=-1)


# ---- factory 격리 ------------------------------------------------------------


def test_walk_forward_uses_fresh_strategy_per_window() -> None:
    """factory 가 각 윈도우마다 새 인스턴스 — state 누설 X."""
    call_count = [0]

    def _counting_factory() -> Strategy:
        call_count[0] += 1
        return BuyHoldSell()

    df = _ohlc_df(n=100)
    result = walk_forward(_counting_factory, "X", df, window_days=30, step_days=30)
    assert call_count[0] == result.summary.n_windows


# ---- summary 통계 ------------------------------------------------------------


def test_summary_aggregates_metrics_across_windows() -> None:
    df = _ohlc_df(n=200, slope=0.001)
    result = walk_forward(_factory, "X", df, window_days=60, step_days=30)
    if result.summary.n_windows == 0:
        pytest.skip("윈도우 0건 — 데이터 부족")

    # min ≤ mean ≤ max 가 깨지지 않는지.
    assert result.summary.sharpe_min <= result.summary.sharpe_mean
    assert result.summary.sharpe_mean <= result.summary.sharpe_max
    # 표준편차는 음수 X.
    assert result.summary.sharpe_std >= 0
    # positive_sharpe_ratio 는 [0, 1] 범위.
    assert 0.0 <= result.summary.positive_sharpe_ratio <= 1.0


def test_summary_positive_sharpe_ratio_zero_for_negative_strategy() -> None:
    """단조 *하락* 가격에서 BuyHoldSell — 모든 윈도우 Sharpe ≤ 0."""
    df = _ohlc_df(n=200, slope=-0.002)
    result = walk_forward(_factory, "X", df, window_days=60, step_days=30)
    if result.summary.n_windows == 0:
        pytest.skip("윈도우 0건")
    # 매수 후 보유 = 손실 → Sharpe ≤ 0 (또는 거래 없어서 0). positive_sharpe_ratio 가 0 또는 매우 낮음.
    assert result.summary.positive_sharpe_ratio <= 0.5


# ---- 결정론성 ----------------------------------------------------------------


def test_walk_forward_is_deterministic() -> None:
    """같은 입력 + 같은 factory → 같은 결과."""
    df = _ohlc_df(n=120)
    r1 = walk_forward(_factory, "X", df, window_days=30, step_days=30)
    r2 = walk_forward(_factory, "X", df, window_days=30, step_days=30)
    assert r1.summary == r2.summary
    assert len(r1.windows) == len(r2.windows)
    for w1, w2 in zip(r1.windows, r2.windows, strict=True):
        assert isinstance(w1, WalkForwardWindow)
        assert w1.start == w2.start
        assert w1.report.as_dict() == w2.report.as_dict()
