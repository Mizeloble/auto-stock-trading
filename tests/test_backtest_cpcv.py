"""Purged K-Fold CV — 29단계 PR36 (#2).

분할 정확성 / purge 동작 / 분포 통계 / 가장자리.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from auto_trading.backtest.cpcv import CPCVResult, purged_kfold
from auto_trading.strategy.base import Bar, Side, Signal, Strategy


class _AlwaysBuyOnce(Strategy):
    """첫 봉에 매수 — 결정론. fold 별 결과 분포 확인용."""

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
    """완만한 상승 시계열."""
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


# ---- 분할 ------------------------------------------------------------------


def test_purged_kfold_splits_into_n_folds() -> None:
    """100 봉 + n_folds=10 → 10 fold 모두 생성."""
    df = _df(100)
    result = purged_kfold(_factory, "X", df, n_folds=10)
    assert result.summary.n_folds == 10
    assert len(result.folds) == 10
    # fold 인덱스가 0..9 까지 순서대로.
    assert [f.index for f in result.folds] == list(range(10))


def test_purged_kfold_last_fold_absorbs_remainder() -> None:
    """105 봉 + n_folds=10 → fold 0..8 은 10 봉, 마지막은 15 봉."""
    df = _df(105)
    result = purged_kfold(_factory, "X", df, n_folds=10)
    assert result.folds[-1].n_bars == 15  # 잔여분 흡수
    assert result.folds[0].n_bars == 10


def test_purged_kfold_embargo_removes_boundary_bars() -> None:
    """embargo_bars=2 → 각 fold 양 끝 2 봉씩 제거 → 봉 수 4개 감소."""
    df = _df(100)
    no_embargo = purged_kfold(_factory, "X", df, n_folds=10, embargo_bars=0)
    with_embargo = purged_kfold(_factory, "X", df, n_folds=10, embargo_bars=2)
    # 각 fold 가 4 봉씩 작아져야 (양 끝 2 봉 X 2 = 4).
    assert with_embargo.folds[0].n_bars == no_embargo.folds[0].n_bars - 4


# ---- 분포 통계 --------------------------------------------------------------


def test_purged_kfold_returns_cpcv_result() -> None:
    df = _df(60)
    result = purged_kfold(_factory, "X", df, n_folds=6)
    assert isinstance(result, CPCVResult)
    s = result.summary
    assert s.n_folds == 6
    # 분포 통계 필드 모두 채워짐.
    assert s.sharpe_p05 <= s.sharpe_p50 <= s.sharpe_p95
    assert 0.0 <= s.positive_sharpe_ratio <= 1.0
    assert 0.0 <= s.psr_mean <= 1.0


def test_purged_kfold_positive_sharpe_ratio_high_for_uptrend() -> None:
    """완만한 상승 추세에서 buy-and-hold → 대부분 fold 가 양수 Sharpe."""
    df = _df(120, slope=0.01)  # 0.01/일 ≈ 강한 상승
    result = purged_kfold(_factory, "X", df, n_folds=10)
    # 모든 fold 가 buy → 결과적 positive ratio 가 높아야.
    assert result.summary.positive_sharpe_ratio > 0.5


# ---- 가장자리 --------------------------------------------------------------


def test_purged_kfold_empty_df_returns_empty() -> None:
    result = purged_kfold(_factory, "X", pd.DataFrame(), n_folds=5)
    assert result.summary.n_folds == 0
    assert result.folds == []


def test_purged_kfold_n_folds_one_raises() -> None:
    with pytest.raises(ValueError, match="2 이상"):
        purged_kfold(_factory, "X", _df(100), n_folds=1)


def test_purged_kfold_negative_embargo_raises() -> None:
    with pytest.raises(ValueError, match="음수"):
        purged_kfold(_factory, "X", _df(100), n_folds=5, embargo_bars=-1)


def test_purged_kfold_excessive_embargo_raises() -> None:
    """fold 크기보다 embargo 가 크면 명확한 에러."""
    df = _df(50)
    # fold_size=5, embargo=10 → 5 < 2*10+4=24 → fail
    with pytest.raises(ValueError, match="embargo"):
        purged_kfold(_factory, "X", df, n_folds=10, embargo_bars=10)


def test_purged_kfold_non_datetime_index_raises() -> None:
    df = pd.DataFrame(
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1]}
    )  # RangeIndex
    with pytest.raises(ValueError, match="DatetimeIndex"):
        purged_kfold(_factory, "X", df, n_folds=2)


# ---- 결정론 ----------------------------------------------------------------


def test_purged_kfold_deterministic_for_same_input() -> None:
    """같은 입력 → 같은 fold 결과 (백테스트 결정론 + 분할 결정론)."""
    df = _df(80)
    r1 = purged_kfold(_factory, "X", df, n_folds=8)
    r2 = purged_kfold(_factory, "X", df, n_folds=8)
    assert r1.summary == r2.summary
    assert len(r1.folds) == len(r2.folds)
    for f1, f2 in zip(r1.folds, r2.folds, strict=True):
        assert f1.report.sharpe == f2.report.sharpe
        assert f1.n_bars == f2.n_bars
