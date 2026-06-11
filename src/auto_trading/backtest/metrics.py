"""백테스트 성과 지표."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from math import erf

import numpy as np
import pandas as pd

from ..strategy.base import Side
from .broker import Trade


def _standard_normal_cdf(x: float) -> float:
    """scipy 의존성 회피 — math.erf 만으로 표준정규 CDF."""
    return 0.5 * (1.0 + erf(x / (2**0.5)))


def probabilistic_sharpe_ratio(
    returns: Iterable[float],
    *,
    benchmark_sr: float = 0.0,
) -> float:
    """관측된 Sharpe 가 ``benchmark_sr`` 보다 클 *확률* — 29단계 PR31 (#3).

    Bailey & Lopez de Prado (2012, "The Sharpe Ratio Efficient Frontier") 의
    Probabilistic Sharpe Ratio.

    PSR(SR*) = Φ((SR - SR*) * sqrt(n-1) / sqrt(1 - skew·SR + (kurt - 1)/4 · SR²))

    - ``returns`` 는 *per-period* (일별/분별 등). benchmark_sr 도 같은 주기.
    - 분포가 비대칭(왜도)·뚱뚱한 꼬리(첨도) 인 경우 표본 Sharpe 가 부풀려진 정도를
      보정해 *"진짜 SR > SR* 일 확률"* 을 반환.
    - n < 2 → 0.0 (정보 부족). std=0 → mean>0 면 1.0 else 0.0.

    어떻게 확인: tests/test_backtest_metrics_psr.py 의 known-case 검증.
    """
    arr = np.asarray(list(returns), dtype=float)
    arr = arr[~np.isnan(arr)]
    n = arr.size
    if n < 2:
        return 0.0
    mean = float(arr.mean())
    std = float(arr.std(ddof=1))
    if std == 0.0:
        return 1.0 if mean > benchmark_sr * (std if std else 1.0) else 0.0
    sr = mean / std
    centered = arr - mean
    m2 = float((centered**2).mean())
    if m2 == 0.0:
        return 1.0 if sr > benchmark_sr else 0.0
    m3 = float((centered**3).mean())
    m4 = float((centered**4).mean())
    skew = m3 / m2**1.5
    kurt = m4 / m2**2  # Pearson kurtosis (정규분포 = 3, 초과첨도 X)
    var_term = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr**2
    if var_term <= 0.0:
        return 0.0
    denom = (var_term / (n - 1)) ** 0.5
    if denom == 0.0:
        return 1.0 if sr > benchmark_sr else 0.0
    z = (sr - benchmark_sr) / denom
    return _standard_normal_cdf(z)


@dataclass
class PerformanceReport:
    initial_equity: float
    final_equity: float
    total_return: float  # 0.10 = +10%
    cagr: float
    annualized_vol: float
    sharpe: float  # 연환산
    max_drawdown: float  # 음수 (-0.15 = -15%)
    win_rate: float  # 0~1
    num_trades: int
    num_round_trips: int
    # 29단계 PR31 (#3) — 진짜 Sharpe > 0 일 확률 (0~1).
    # 표본 적거나 분포 비대칭/뚱뚱한 꼬리면 1.2 같은 점추정도 운으로 나올 수 있음.
    # 0.5 ≈ "운/실력 구분 안 됨", 0.95+ = "통계적으로 유의미".
    probabilistic_sharpe: float = 0.0

    def as_dict(self) -> dict:
        return {
            "initial_equity": round(self.initial_equity, 2),
            "final_equity": round(self.final_equity, 2),
            "total_return_pct": round(self.total_return * 100, 2),
            "cagr_pct": round(self.cagr * 100, 2),
            "annualized_vol_pct": round(self.annualized_vol * 100, 2),
            "sharpe": round(self.sharpe, 3),
            "probabilistic_sharpe": round(self.probabilistic_sharpe, 3),
            "max_drawdown_pct": round(self.max_drawdown * 100, 2),
            "win_rate_pct": round(self.win_rate * 100, 2),
            "num_trades": self.num_trades,
            "num_round_trips": self.num_round_trips,
        }


_TRADING_DAYS_PER_YEAR = 252
_TRADING_MINUTES_PER_DAY = 390  # KRX 09:00~15:30 연속 거래


def _periods_per_year(index: pd.Index) -> float:
    """봉 간격에서 연환산 계수 추정 — 2026-06-10 제3자 검토.

    종전엔 x252 하드코딩이라 1분봉 equity curve 를 넣으면 Sharpe·변동성이
    sqrt(390)≈19.7배 부풀려졌다. 봉 간격의 *중앙값* 으로 추정 — 야간/주말 갭이
    섞여도 대부분의 diff 는 봉 간격이라 중앙값이 견고하다.
    반일(43,200초) 이상이면 일봉 이상으로 보고 기존과 동일하게 252.
    """
    if len(index) < 3:
        return float(_TRADING_DAYS_PER_YEAR)
    try:
        diffs = np.diff(index.values).astype("timedelta64[s]").astype(float)
    except TypeError, ValueError:
        return float(_TRADING_DAYS_PER_YEAR)
    diffs = diffs[diffs > 0]
    if diffs.size == 0:
        return float(_TRADING_DAYS_PER_YEAR)
    median_sec = float(np.median(diffs))
    if median_sec >= 43_200:  # 반일 이상 → 일봉·주봉 등은 기존 동작(252) 유지
        return float(_TRADING_DAYS_PER_YEAR)
    bars_per_day = _TRADING_MINUTES_PER_DAY * 60.0 / median_sec
    return float(_TRADING_DAYS_PER_YEAR) * bars_per_day


def compute(
    equity_curve: pd.Series, trades: list[Trade], initial_equity: float
) -> PerformanceReport:
    if equity_curve.empty:
        return PerformanceReport(initial_equity, initial_equity, 0, 0, 0, 0, 0, 0, 0, 0)

    final_equity = float(equity_curve.iloc[-1])
    total_return = final_equity / initial_equity - 1.0

    # 연환산 지표 — 봉 간격을 인덱스에서 추정 (일봉=252, 분봉=252 x 하루 봉수)
    daily = equity_curve.pct_change().dropna()
    periods = _periods_per_year(equity_curve.index)
    ann_vol = float(daily.std() * np.sqrt(periods)) if len(daily) > 1 else 0.0
    ann_ret = float(daily.mean() * periods) if len(daily) > 1 else 0.0
    sharpe = ann_ret / ann_vol if ann_vol > 0 else 0.0
    # 29단계 PR31 — Probabilistic Sharpe (per-period 수익률·SR* = 0 기준).
    psr = probabilistic_sharpe_ratio(daily.tolist(), benchmark_sr=0.0)

    # CAGR — 기간을 초 단위로 계산 (종전 .days 는 하루 미만 분봉 곡선에서 0 → 1e-9 년
    # 으로 떨어져 지수가 10^9 이 되는 OverflowError). 최소 1일로 클램프해 일중 곡선의
    # CAGR 폭주를 막는다 (그래도 극단값이면 inf 로 수렴시키고 죽지 않게).
    span_sec = float((equity_curve.index[-1] - equity_curve.index[0]).total_seconds())
    years = max(span_sec / (365.25 * 86_400), 1 / 365.25)
    try:
        cagr = (final_equity / initial_equity) ** (1 / years) - 1 if initial_equity > 0 else 0.0
    except OverflowError:
        cagr = float("inf")

    # MDD
    running_max = equity_curve.cummax()
    drawdown = equity_curve / running_max - 1.0
    max_dd = float(drawdown.min()) if len(drawdown) else 0.0

    # 라운드트립 승률 계산 (FIFO)
    round_trips = _round_trips(trades)
    wins = sum(1 for pnl in round_trips if pnl > 0)
    win_rate = wins / len(round_trips) if round_trips else 0.0

    return PerformanceReport(
        initial_equity=initial_equity,
        final_equity=final_equity,
        total_return=total_return,
        cagr=cagr,
        annualized_vol=ann_vol,
        sharpe=sharpe,
        max_drawdown=max_dd,
        win_rate=win_rate,
        num_trades=len(trades),
        num_round_trips=len(round_trips),
        probabilistic_sharpe=psr,
    )


def _round_trips(trades: list[Trade]) -> list[float]:
    """FIFO 매칭으로 매수→매도 쌍의 순손익을 계산."""
    from collections import defaultdict, deque

    opens: dict[str, deque] = defaultdict(deque)
    pnls: list[float] = []
    for t in trades:
        if t.side is Side.BUY:
            # 매수 수수료는 *매수 lot 수량* 기준 주당 단가로 보관 — 매도 수량 분모로
            # 안분하면 피라미딩 시 과소, 부분 매도 시 중복 차감돼 승률이 왜곡된다.
            fee_per_share = t.fee / t.quantity if t.quantity else 0.0
            opens[t.symbol].append([t.quantity, t.price, fee_per_share])
        else:
            remaining = t.quantity
            sell_cost = t.fee + t.tax
            while remaining > 0 and opens[t.symbol]:
                buy_qty, buy_price, buy_fee_per_share = opens[t.symbol][0]
                matched = min(buy_qty, remaining)
                pnl = (t.price - buy_price) * matched
                pnl -= buy_fee_per_share * matched
                pnl -= sell_cost * (matched / t.quantity if t.quantity else 0)
                pnls.append(pnl)
                buy_qty -= matched
                remaining -= matched
                if buy_qty == 0:
                    opens[t.symbol].popleft()
                else:
                    opens[t.symbol][0][0] = buy_qty
    return pnls
