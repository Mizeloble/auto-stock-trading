"""Combinatorial Purged K-Fold — 29단계 PR38 (#2 Phase 2)."""

from __future__ import annotations

from datetime import datetime, timedelta
from math import comb

import pandas as pd
import pytest

from auto_trading.backtest.cpcv import (
    CombinatorialResult,
    combinatorial_purged_kfold,
)
from auto_trading.strategy.base import Bar, Side, Signal, Strategy


class _AlwaysBuyOnce(Strategy):
    def __init__(self) -> None:
        super().__init__()
        self._done = False

    def on_bar(self, bar: Bar) -> list[Signal]:
        if not self._done:
            self._done = True
            return [Signal(symbol=bar.symbol, side=Side.BUY, weight=1.0)]
        return []

    def reset(self) -> None:
        super().reset()
        self._done = False


def _factory() -> Strategy:
    return _AlwaysBuyOnce()


def _df(n: int, *, slope: float = 0.005) -> pd.DataFrame:
    rows = []
    for i in range(n):
        price = 100.0 * (1 + slope * i)
        rows.append(
            {
                "ts": datetime(2026, 1, 1) + timedelta(days=i),
                "open": price,
                "high": price * 1.005,
                "low": price * 0.995,
                "close": price,
                "volume": 1_000,
            }
        )
    return pd.DataFrame(rows).set_index("ts")


# ---- 조합 개수 ------------------------------------------------------------


def test_combinatorial_enumerates_all_combinations() -> None:
    """N=6, k=2 → C(6,2)=15 조합 모두 평가."""
    df = _df(120)
    result = combinatorial_purged_kfold(_factory, "X", df, n_folds=6, n_test_folds=2)
    assert result.n_combinations == comb(6, 2)
    assert len(result.folds) == comb(6, 2)
    # 모든 조합이 정렬된 인덱스 튜플.
    for f in result.folds:
        assert tuple(sorted(f.combination)) == f.combination


def test_combinatorial_k1_equals_phase1_count() -> None:
    """n_test_folds=1 → fold 개수 = n_folds (Phase 1 과 같은 분포)."""
    df = _df(60)
    result = combinatorial_purged_kfold(_factory, "X", df, n_folds=6, n_test_folds=1)
    assert result.n_combinations == 6


def test_combinatorial_k_equal_n_yields_one_combo() -> None:
    """n_test_folds=N → 단 1개 조합 (전체 fold 합쳐 1 백테스트)."""
    df = _df(60)
    result = combinatorial_purged_kfold(_factory, "X", df, n_folds=6, n_test_folds=6)
    assert result.n_combinations == 1


# ---- 조합 콘텐츠 ----------------------------------------------------------


def test_combinatorial_folds_are_time_sorted_concat() -> None:
    """각 조합의 combined df 는 시간순 — start < end."""
    df = _df(60)
    result = combinatorial_purged_kfold(_factory, "X", df, n_folds=6, n_test_folds=3)
    for f in result.folds:
        assert f.start < f.end
        assert f.n_bars > 0


def test_combinatorial_returns_cpcv_summary_with_distribution() -> None:
    """CPCVSummary 재사용 — 분포 통계 동일하게 계산."""
    df = _df(80)
    result = combinatorial_purged_kfold(_factory, "X", df, n_folds=8, n_test_folds=2)
    assert isinstance(result, CombinatorialResult)
    s = result.summary
    assert s.n_folds == result.n_combinations  # summary 의 n_folds = 표본 크기 (= 조합 개수)
    assert s.sharpe_p05 <= s.sharpe_p50 <= s.sharpe_p95
    assert 0.0 <= s.positive_sharpe_ratio <= 1.0


# ---- 가장자리 -------------------------------------------------------------


def test_combinatorial_empty_df_returns_empty() -> None:
    result = combinatorial_purged_kfold(_factory, "X", pd.DataFrame(), n_folds=5, n_test_folds=2)
    assert result.n_combinations == 0
    assert result.folds == []


def test_combinatorial_invalid_n_test_folds_raises() -> None:
    df = _df(60)
    # 0 또는 음수
    with pytest.raises(ValueError, match="n_test_folds"):
        combinatorial_purged_kfold(_factory, "X", df, n_folds=6, n_test_folds=0)
    # n_folds 초과
    with pytest.raises(ValueError, match="n_test_folds"):
        combinatorial_purged_kfold(_factory, "X", df, n_folds=6, n_test_folds=7)


def test_combinatorial_excessive_embargo_raises() -> None:
    df = _df(50)
    with pytest.raises(ValueError, match="embargo"):
        combinatorial_purged_kfold(_factory, "X", df, n_folds=10, n_test_folds=2, embargo_bars=10)


def test_combinatorial_deterministic_for_same_input() -> None:
    df = _df(60)
    r1 = combinatorial_purged_kfold(_factory, "X", df, n_folds=5, n_test_folds=2)
    r2 = combinatorial_purged_kfold(_factory, "X", df, n_folds=5, n_test_folds=2)
    assert r1.summary == r2.summary
    assert [f.combination for f in r1.folds] == [f.combination for f in r2.folds]
    for a, b in zip(r1.folds, r2.folds, strict=True):
        assert a.report.sharpe == b.report.sharpe
