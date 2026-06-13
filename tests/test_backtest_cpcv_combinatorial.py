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


# ---- 2026-06-14 제3자 검토 — fold 경계 가격 갭이 가짜 PnL 을 안 만드는지 ----------


def _df_with_price_levels(n: int, levels: list[float]) -> pd.DataFrame:
    """구간별로 가격대가 다른 df — 비연속 fold 경계의 점프를 재현하기 위함.

    levels 길이로 n 봉을 등분, 각 구간은 해당 가격대에서 +0.5%/봉 완만 상승.
    예: levels=[100, 5000] → 전반부 100 근처, 후반부 5000 근처 (경계에서 50배 점프).
    """
    rows = []
    seg = n // len(levels)
    for i in range(n):
        base = levels[min(i // seg, len(levels) - 1)]
        price = base * (1 + 0.005 * (i % seg))
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


def test_boundary_price_jump_does_not_create_phantom_return() -> None:
    """가격대가 50배 다른 두 구간을 buy-and-hold 로 묶어도 수익이 폭발하지 않는다.

    종전(pd.concat 후 한 번에 백테스트)에는 저가 fold→고가 fold 경계의 가격 점프가
    그대로 +수천% 가짜 수익이 됐다. 각 fold 독립 백테스트(자본만 연속)로 차단.
    각 구간 내부 상승은 +0.5%/봉 뿐이라 어떤 조합도 현실적 범위(<2배)에 머물러야 한다.
    """
    # 4 구간: 100 / 5000 / 200 / 8000 — 인접·비인접 경계 모두 큰 점프 발생.
    df = _df_with_price_levels(160, [100.0, 5000.0, 200.0, 8000.0])
    result = combinatorial_purged_kfold(
        _factory, "X", df, n_folds=4, n_test_folds=2, slippage_bps=0.0
    )
    assert result.n_combinations == comb(4, 2)
    for f in result.folds:
        # 경계 갭이 샜다면 total_return 이 수십~수백 배로 튄다. 현실 상승만이면 작다.
        assert f.report.total_return < 1.0, (
            f"조합 {f.combination} total_return={f.report.total_return:.2f} — 경계 갭 누수 의심"
        )


def test_boundary_reset_clears_strategy_state() -> None:
    """fold 마다 전략 인스턴스가 새로 생성돼 내부 상태(보유 여부)가 reset 된다.

    _AlwaysBuyOnce 는 fold 당 첫 봉에서 1회 매수 → k개 fold 면 매수 trade 가 k건.
    종전 concat 방식은 전체에서 1건만 매수했다(상태가 안 끊김).
    """
    df = _df(120)
    result = combinatorial_purged_kfold(_factory, "X", df, n_folds=6, n_test_folds=3)
    # 모든 조합에서 num_trades 가 최소 3 (fold 당 1매수) 이상 — 경계 reset 증거.
    assert result.folds
    for f in result.folds:
        assert f.report.num_trades >= 3
