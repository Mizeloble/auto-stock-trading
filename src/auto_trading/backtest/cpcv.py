"""Purged Cross-Validation (CPCV Phase 1) — 29단계 PR36 (#2).

무엇/왜:
- ``walk_forward`` 는 슬라이딩 윈도우 — 겹침 허용·step 가변. 통계검정 보단 "시간에
  따른 일관성" 가시화에 적합.
- 이 모듈은 *균등 N-fold 분할 + purge* 로 **분포 자체** 를 생산. Lopez de Prado 의
  Purged K-Fold CV 핵심 (Wikipedia: "Purged cross-validation"). 학습 단계가 없는
  결정론 백테스트라 train 분리는 생략 — fold 별 평가만으로 분포 신뢰구간 산출.
- Sharpe 1.2 점추정 + PSR (PR31) 에 더해 *fold-별 분포* 가 또 다른 운/실력 검증층.

이번 PR (Phase 1):
- N-fold 균등 분할 + ``embargo_bars`` purge.
- 각 fold 단독 백테스트 → Sharpe / PSR / MDD / win_rate 분포.

다음 PR (Phase 2, 분리):
- combinatorial — ``n_test_folds=k`` 일반화. C(N, k) 조합 모두 시뮬 → 더 큰 표본.

CLI 결합 (``auto-trade backtest --cpcv``) 은 별도 PR.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from ..strategy.base import Strategy
from .engine import Backtester
from .metrics import PerformanceReport

__all__ = [
    "CPCVFold",
    "CPCVResult",
    "CPCVSummary",
    "purged_kfold",
]


@dataclass(frozen=True)
class CPCVFold:
    """단일 fold 백테스트 결과."""

    index: int  # 0-based
    start: datetime
    end: datetime
    n_bars: int
    report: PerformanceReport


@dataclass(frozen=True)
class CPCVSummary:
    """fold 분포 요약 — 백분위까지 포함해 신뢰구간 답변."""

    n_folds: int
    sharpe_mean: float
    sharpe_std: float
    sharpe_p05: float  # 5th percentile
    sharpe_p50: float  # median
    sharpe_p95: float  # 95th percentile
    positive_sharpe_ratio: float
    psr_mean: float  # PR31 — 일관성 확률 평균
    mdd_worst_pct: float
    win_rate_mean_pct: float


@dataclass(frozen=True)
class CPCVResult:
    folds: list[CPCVFold]
    summary: CPCVSummary


def purged_kfold(
    strategy_factory: Callable[[], Strategy],
    symbol: str,
    df: pd.DataFrame,
    *,
    n_folds: int = 10,
    embargo_bars: int = 0,
    initial_cash: float = 10_000_000.0,
    position_sizing: str = "all_in",
    atr_period: int = 14,
    atr_risk_pct: float = 0.01,
    commission_rate: float = 0.00015,
    tax_rate_sell: float = 0.0020,
    slippage_bps: float = 5.0,
    slippage_model: str = "fixed",
    impact_coefficient: float = 2.0,
) -> CPCVResult:
    """N-fold 균등 시간 분할 + purge → fold 별 백테스트 → 분포 요약.

    매개변수:
    - ``n_folds``: 분할 개수. 10 권장 (분포 표본 충분 + 각 fold 길이 합리적).
    - ``embargo_bars``: 각 fold 양 끝에서 잘라낼 봉 수. 인접 fold 경계의 인공 효과
      차단 (개시·청산이 fold 경계에 몰리는 부수효과 등). 기본 0 — 사용자 선택.
    - ``slippage_model`` / ``impact_coefficient`` (PR35): impact 모델 사용 시 분포가
      더 보수적으로 나옴.

    동작:
    - df 를 길이 기준 ``n_folds`` 등분. 마지막 fold 는 잔여분 흡수 (균등 안 떨어질 때).
    - 각 fold 의 처음·끝 ``embargo_bars`` 만큼 제거 후 백테스트.
    - fold 가 4 봉 미만이면 백테스트 의미 X → skip.
    """
    if n_folds <= 1:
        raise ValueError(f"n_folds 는 2 이상이어야 함: {n_folds}")
    if embargo_bars < 0:
        raise ValueError(f"embargo_bars 는 음수 불가: {embargo_bars}")
    if df.empty:
        return CPCVResult(folds=[], summary=_empty_summary())
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError("df.index 는 DatetimeIndex 여야 합니다")

    n = len(df)
    fold_size = n // n_folds
    if fold_size <= 2 * embargo_bars + 4:
        raise ValueError(
            f"fold 크기({fold_size}) 가 embargo*2+4({2 * embargo_bars + 4}) 보다 작음 — "
            f"n_folds 줄이거나 embargo_bars 줄여라"
        )

    folds: list[CPCVFold] = []
    for i in range(n_folds):
        start_idx = i * fold_size
        # 마지막 fold 는 잔여분 흡수.
        end_idx = n if i == n_folds - 1 else (i + 1) * fold_size
        # purge: 양 끝에서 embargo_bars 제거.
        slice_start = start_idx + embargo_bars
        slice_end = end_idx - embargo_bars
        slice_df = df.iloc[slice_start:slice_end]
        if len(slice_df) < 4:
            continue

        bt = Backtester(
            strategy=strategy_factory(),
            initial_cash=initial_cash,
            position_sizing=position_sizing,
            atr_period=atr_period,
            atr_risk_pct=atr_risk_pct,
            commission_rate=commission_rate,
            tax_rate_sell=tax_rate_sell,
            slippage_bps=slippage_bps,
            slippage_model=slippage_model,
            impact_coefficient=impact_coefficient,
        )
        result = bt.run(symbol, slice_df)
        folds.append(
            CPCVFold(
                index=i,
                start=slice_df.index[0].to_pydatetime(),
                end=slice_df.index[-1].to_pydatetime(),
                n_bars=len(slice_df),
                report=result.report,
            )
        )

    return CPCVResult(folds=folds, summary=_summarize(folds))


def _percentile(sorted_values: list[float], pct: float) -> float:
    """선형 보간 percentile (numpy 없이)."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = pct / 100.0 * (len(sorted_values) - 1)
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = k - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def _summarize(folds: list[CPCVFold]) -> CPCVSummary:
    if not folds:
        return _empty_summary()
    sharpes = [f.report.sharpe for f in folds]
    psrs = [f.report.probabilistic_sharpe for f in folds]
    mdds = [f.report.max_drawdown * 100.0 for f in folds]
    win_rates = [f.report.win_rate * 100.0 for f in folds]
    sorted_sharpes = sorted(sharpes)
    positive = sum(1 for s in sharpes if s > 0)

    return CPCVSummary(
        n_folds=len(folds),
        sharpe_mean=round(statistics.fmean(sharpes), 3),
        sharpe_std=round(statistics.pstdev(sharpes) if len(sharpes) > 1 else 0.0, 3),
        sharpe_p05=round(_percentile(sorted_sharpes, 5), 3),
        sharpe_p50=round(_percentile(sorted_sharpes, 50), 3),
        sharpe_p95=round(_percentile(sorted_sharpes, 95), 3),
        positive_sharpe_ratio=round(positive / len(sharpes), 3),
        psr_mean=round(statistics.fmean(psrs), 3),
        mdd_worst_pct=round(min(mdds), 2),
        win_rate_mean_pct=round(statistics.fmean(win_rates), 2),
    )


def _empty_summary() -> CPCVSummary:
    return CPCVSummary(
        n_folds=0,
        sharpe_mean=0.0,
        sharpe_std=0.0,
        sharpe_p05=0.0,
        sharpe_p50=0.0,
        sharpe_p95=0.0,
        positive_sharpe_ratio=0.0,
        psr_mean=0.0,
        mdd_worst_pct=0.0,
        win_rate_mean_pct=0.0,
    )
