"""Walk-Forward 백테스트 — 슬라이딩 윈도우로 시간을 나눠 평가.

29단계 PR23 (#1) — 단일 백테스트의 "운 좋은 구간" 함정을 차단한다.
전체 N년 결과 한 줄로 보면 그 안의 5년이 운 좋아서 나머지 1년 손실을
가려도 못 잡지만, walk-forward 는 윈도우별로 결과를 분리해 분포로 보여줌.

설계 단순화:
- 이번 PR 은 *시간 분할만* — parameter optimization (train 구간 사용) 은
  별도 PR 로 분리. 윈도우별 결과 분포만으로도 "전략이 시간에 따라 일관적인가?"
  질문에 답함.
- 호출자가 ``strategy_factory: Callable[[], Strategy]`` 를 넘김 — 윈도우마다
  새 인스턴스 (state reset 자동, 기존 인스턴스 공유 시 학습 누설 위험).
- 각 윈도우는 독립 Backtester. 결과는 ``WalkForwardResult`` 로 묶음.

CLI 통합 (``auto-trade backtest --walk-forward``) 은 PR24 로 분리.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from ..strategy.base import Strategy
from .broker import Trade
from .engine import Backtester
from .metrics import PerformanceReport

__all__ = [
    "WalkForwardResult",
    "WalkForwardSummary",
    "WalkForwardWindow",
    "walk_forward",
]


@dataclass(frozen=True)
class WalkForwardWindow:
    """단일 윈도우 백테스트 결과 + 시작/끝 timestamp."""

    start: datetime
    end: datetime
    report: PerformanceReport
    trades: list[Trade]


@dataclass(frozen=True)
class WalkForwardSummary:
    """전체 윈도우의 메트릭 분포 요약. 분산 정보가 단일 백테스트와의 차이."""

    n_windows: int
    sharpe_mean: float
    sharpe_std: float
    sharpe_min: float
    sharpe_max: float
    cagr_mean_pct: float
    cagr_std_pct: float
    mdd_worst_pct: float  # 가장 안 좋은 MDD (가장 작은 음수)
    win_rate_mean_pct: float
    # 윈도우 중 Sharpe > 0 인 비율. 일관성 지표 — 1.0 에 가까울수록 안정적.
    positive_sharpe_ratio: float


@dataclass(frozen=True)
class WalkForwardResult:
    """walk_forward() 의 최종 반환. 윈도우 리스트 + 합산 요약."""

    windows: list[WalkForwardWindow]
    summary: WalkForwardSummary


def walk_forward(
    strategy_factory: Callable[[], Strategy],
    symbol: str,
    df: pd.DataFrame,
    *,
    window_days: int,
    step_days: int,
    initial_cash: float = 10_000_000.0,
    position_sizing: str = "all_in",
    atr_period: int = 14,
    atr_risk_pct: float = 0.01,
    commission_rate: float = 0.00015,
    tax_rate_sell: float = 0.0020,
    slippage_bps: float = 5.0,
) -> WalkForwardResult:
    """슬라이딩 윈도우로 백테스트.

    df: bars_to_dataframe 결과 — DatetimeIndex 오름차순.
    window_days: 한 윈도우의 길이 (예: 90).
    step_days: 다음 윈도우까지 간격 (예: 30 → 윈도우 67% 겹침, 30 → 0%).

    예: 1년 데이터 + window=90 + step=30 → 약 10개 윈도우.
    """
    if window_days <= 0 or step_days <= 0:
        raise ValueError("window_days / step_days 는 양수여야 합니다")
    if df.empty:
        return WalkForwardResult(windows=[], summary=_empty_summary())

    windows: list[WalkForwardWindow] = []

    # DatetimeIndex 가정. tz-naive / tz-aware 둘 다 처리.
    timestamps = df.index
    if not isinstance(timestamps, pd.DatetimeIndex):
        raise ValueError("df.index 는 DatetimeIndex 여야 합니다")

    start_ts = timestamps[0]
    end_ts = timestamps[-1]

    window_delta = pd.Timedelta(days=window_days)
    step_delta = pd.Timedelta(days=step_days)

    cursor = start_ts
    while cursor + window_delta <= end_ts + pd.Timedelta(days=1):
        window_end = cursor + window_delta
        slice_df = df.loc[(df.index >= cursor) & (df.index < window_end)]
        if len(slice_df) < 2:
            cursor += step_delta
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
        )
        result = bt.run(symbol, slice_df)
        windows.append(
            WalkForwardWindow(
                start=slice_df.index[0].to_pydatetime(),
                end=slice_df.index[-1].to_pydatetime(),
                report=result.report,
                trades=result.trades,
            )
        )
        cursor += step_delta

    summary = _summarize(windows)
    return WalkForwardResult(windows=windows, summary=summary)


def _summarize(windows: list[WalkForwardWindow]) -> WalkForwardSummary:
    if not windows:
        return _empty_summary()

    sharpes = [w.report.sharpe for w in windows]
    cagrs = [w.report.cagr * 100.0 for w in windows]
    mdds = [w.report.max_drawdown * 100.0 for w in windows]
    win_rates = [w.report.win_rate * 100.0 for w in windows]
    positive = sum(1 for s in sharpes if s > 0)

    return WalkForwardSummary(
        n_windows=len(windows),
        sharpe_mean=round(statistics.fmean(sharpes), 3),
        sharpe_std=round(statistics.pstdev(sharpes) if len(sharpes) > 1 else 0.0, 3),
        sharpe_min=round(min(sharpes), 3),
        sharpe_max=round(max(sharpes), 3),
        cagr_mean_pct=round(statistics.fmean(cagrs), 2),
        cagr_std_pct=round(statistics.pstdev(cagrs) if len(cagrs) > 1 else 0.0, 2),
        mdd_worst_pct=round(min(mdds), 2),  # 가장 음수 (= 최악)
        win_rate_mean_pct=round(statistics.fmean(win_rates), 2),
        positive_sharpe_ratio=round(positive / len(sharpes), 3),
    )


def _empty_summary() -> WalkForwardSummary:
    return WalkForwardSummary(
        n_windows=0,
        sharpe_mean=0.0,
        sharpe_std=0.0,
        sharpe_min=0.0,
        sharpe_max=0.0,
        cagr_mean_pct=0.0,
        cagr_std_pct=0.0,
        mdd_worst_pct=0.0,
        win_rate_mean_pct=0.0,
        positive_sharpe_ratio=0.0,
    )
