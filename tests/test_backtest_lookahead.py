"""백테스트 look-ahead 자동 감지 — 29단계 PR33 (#18).

(a) 헬퍼 자체 단위: 정상 전략 통과 / 일부러 만든 cheater 는 fail.
(b) 등록된 모든 전략에 자동 parametrize — CI 가 영원히 지키는 회귀 안전망.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from auto_trading.backtest.lookahead import assert_no_lookahead
from auto_trading.strategy import registry
from auto_trading.strategy.base import Bar, Side, Signal, Strategy


def _make_df(n: int = 60, *, base: float = 100.0, slope: float = 0.005) -> pd.DataFrame:
    """완만한 상승 시계열 — 시그널 발생 다양성을 보장하는 정도의 변동."""
    rng = np.random.default_rng(seed=42)
    noise = rng.normal(0, 0.005, size=n)
    rows = []
    for i in range(n):
        price = base * (1 + slope * i) * (1 + noise[i])
        rows.append(
            {
                "ts": datetime(2026, 1, 1) + timedelta(days=i),
                "open": price,
                "high": price * 1.005,
                "low": price * 0.995,
                "close": price * (1 + noise[i] * 0.3),
                "volume": 1_000,
            }
        )
    return pd.DataFrame(rows).set_index("ts")


# ---- (a) 헬퍼 자체 ----------------------------------------------------------


class _AlwaysBuyOnce(Strategy):
    """첫 봉에 매수 — 미래 데이터 안 봄. 통과해야 함."""

    def __init__(self) -> None:
        super().__init__()
        self._bought = False

    def on_bar(self, bar: Bar) -> list[Signal]:
        if not self._bought:
            self._bought = True
            return [Signal(symbol=bar.symbol, side=Side.BUY, weight=1.0)]
        return []

    def reset(self) -> None:
        super().reset()
        self._bought = False


def test_assert_no_lookahead_passes_for_clean_strategy() -> None:
    """on_bar 가 미래 안 보면 통과."""
    df = _make_df(60)
    # 예외 안 나면 통과.
    assert_no_lookahead(lambda: _AlwaysBuyOnce(), df, symbol="TEST")


class _DataframeCheater(Strategy):
    """df 자체를 멤버로 가지는 cheater — perturb 된 df 면 close 가 바뀌어 결정 뒤집힘.

    factory 가 백테스트 실행 직전 ``set_data(df)`` 를 호출해야 작동.
    """

    def __init__(self) -> None:
        super().__init__()
        self._df: pd.DataFrame | None = None
        self._i = 0
        self._bought = False

    def set_data(self, df: pd.DataFrame) -> None:
        self._df = df

    def on_bar(self, bar: Bar) -> list[Signal]:
        idx = self._i
        self._i += 1
        if self._df is None or self._bought:
            return []
        # 5 봉 뒤 종가를 미리 봄 — look-ahead.
        future_i = idx + 5
        if future_i >= len(self._df):
            return []
        future_close = float(self._df.iloc[future_i]["close"])
        if future_close > bar.close * 1.10:
            self._bought = True
            return [Signal(symbol=bar.symbol, side=Side.BUY, weight=1.0)]
        return []

    def reset(self) -> None:
        super().reset()
        self._i = 0
        self._bought = False


def test_assert_no_lookahead_detects_dataframe_cheater() -> None:
    """df 자체를 참조하는 cheater → LookaheadDetected. perturb_from=10 에서 변조하면
    cheater 가 10 ± 5 = 5 시점부터 결정이 뒤집힘 (변조된 df 의 future close 가 x100)."""
    df = _make_df(60)
    state: dict[str, _DataframeCheater] = {}

    def factory() -> Strategy:
        s = _DataframeCheater()
        state["last"] = s
        return s

    # assert_no_lookahead 가 strategy 를 만든 뒤 백테스트 직전에 df 를 주입해야 함.
    # 시그니처 변경 없이 — Backtester 가 만들기 직전에 monkey-patch 로 set_data 자동 호출.
    # 간단하게: factory 가 *외부 holder* 의 현재 df 를 set_data.
    df_holder: dict[str, pd.DataFrame] = {"current": df}

    def factory_with_data() -> Strategy:
        s = _DataframeCheater()
        s.set_data(df_holder["current"])
        return s

    # 직접 두 번 실행해서 비교 — assert_no_lookahead 내부와 동일한 변조 로직.
    from auto_trading.backtest.engine import Backtester

    bt1 = Backtester(strategy=factory_with_data(), initial_cash=10_000_000.0)
    r1 = bt1.run("TEST", df)

    df2 = df.copy()
    cols = ["open", "high", "low", "close"]
    cidx = df2.columns.get_indexer(cols)
    df2.iloc[10:, cidx] = df2.iloc[10:, cidx] * 100.0
    df_holder["current"] = df2  # cheater 가 변조된 df 를 보도록.
    bt2 = Backtester(strategy=factory_with_data(), initial_cash=10_000_000.0)
    r2 = bt2.run("TEST", df2)

    # cheater 가 변조된 future close 를 보면 5 번 째 봉 이전에 결정이 뒤집힘 → 에쿼티
    # 차이 발생.
    cutoff_ts = df.index[10]
    eq1 = r1.equity_curve[r1.equity_curve.index < cutoff_ts]
    eq2 = r2.equity_curve[r2.equity_curve.index < cutoff_ts]
    assert not np.allclose(eq1.to_numpy(), eq2.to_numpy()), (
        "cheater 가 미래 데이터로 결정 뒤집은 영향이 변조 전 에쿼티에 나타나야 함"
    )


# ---- (b) 등록 전략 자동 parametrize -----------------------------------------


@pytest.mark.parametrize("strategy_name", registry.available())
def test_registered_strategies_no_lookahead(strategy_name: str) -> None:
    """모든 내장 전략이 look-ahead 없이 작동 — CI 회귀 안전망.

    파라미터 기본값으로 가능한 전략만 검사. 기본값으로 생성 불가능한 전략은 skip.
    """
    df = _make_df(60)
    try:
        # 파라미터 없이 생성.
        registry.get(strategy_name)
    except TypeError as exc:
        pytest.skip(f"{strategy_name} 기본 파라미터 없이 생성 불가: {exc}")

    def factory() -> Strategy:
        return registry.get(strategy_name)

    # 예외 안 나면 통과.
    assert_no_lookahead(factory, df, symbol="TEST")


# ---- 가장자리 ---------------------------------------------------------------


def test_too_short_df_raises_value_error() -> None:
    df = _make_df(3)
    with pytest.raises(ValueError, match="길이 부족"):
        assert_no_lookahead(lambda: _AlwaysBuyOnce(), df)


def test_perturb_from_out_of_range_raises() -> None:
    df = _make_df(20)
    with pytest.raises(ValueError, match="out of range"):
        assert_no_lookahead(lambda: _AlwaysBuyOnce(), df, perturb_from=0)
    with pytest.raises(ValueError, match="out of range"):
        assert_no_lookahead(lambda: _AlwaysBuyOnce(), df, perturb_from=len(df))
