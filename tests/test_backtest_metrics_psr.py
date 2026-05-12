"""Probabilistic Sharpe Ratio — 29단계 PR31 (#3).

표본 SR 이 *진짜* SR 인지 운인지 — Bailey & Lopez de Prado (2012).

검증:
- 표본 부족 / 분산 0 / 비정상 분모 등 가장자리
- 양수 평균 + 정규 분포 + 큰 n → PSR ≈ 1
- 작은 n 또는 평균 ≈ 0 → PSR ≈ 0.5
- 음수 평균 → PSR < 0.5
- 음의 왜도(좌측 꼬리 두꺼움) → 같은 SR 보다 PSR 더 낮음
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from auto_trading.backtest.engine import Backtester
from auto_trading.backtest.metrics import compute, probabilistic_sharpe_ratio
from auto_trading.strategy.base import Bar, Signal, Strategy

# ---- 가장자리 ----------------------------------------------------------------


def test_psr_empty_returns_zero() -> None:
    assert probabilistic_sharpe_ratio([]) == 0.0


def test_psr_single_observation_returns_zero() -> None:
    """n < 2 → 정보 부족 → 0.0."""
    assert probabilistic_sharpe_ratio([0.01]) == 0.0


def test_psr_constant_positive_returns_one() -> None:
    """std=0 + 평균 > 0 → 결정론적 양의 SR → 1.0."""
    assert probabilistic_sharpe_ratio([0.01] * 5) == 1.0


def test_psr_constant_zero_returns_zero() -> None:
    """std=0 + 평균=0 → SR 무의미 → 0.0."""
    assert probabilistic_sharpe_ratio([0.0] * 5) == 0.0


# ---- 정규 분포 케이스 (numpy 결정론적 RNG) ----------------------------------


def test_psr_high_with_strong_positive_mean_and_large_n() -> None:
    """평균 명확히 양수 + n=1000 → PSR ≈ 1."""
    rng = np.random.default_rng(seed=42)
    rets = rng.normal(loc=0.005, scale=0.01, size=1000)  # SR per-period ≈ 0.5
    psr = probabilistic_sharpe_ratio(rets)
    assert psr > 0.99


def test_psr_exactly_half_when_mean_zero() -> None:
    """평균을 *정확히* 0 으로 맞추면 SR=0 → z=0 → Φ(0)=0.5."""
    rng = np.random.default_rng(seed=0)
    rets = rng.normal(loc=0.0, scale=0.01, size=500).tolist()
    # 표본 평균을 0 으로 정확히 시프트.
    bias = sum(rets) / len(rets)
    rets = [r - bias for r in rets]
    psr = probabilistic_sharpe_ratio(rets)
    assert abs(psr - 0.5) < 1e-9


def test_psr_below_half_when_mean_negative() -> None:
    """음수 평균 → SR < 0 → PSR < 0.5."""
    rng = np.random.default_rng(seed=1)
    rets = rng.normal(loc=-0.002, scale=0.01, size=500)
    psr = probabilistic_sharpe_ratio(rets)
    assert psr < 0.5


def test_psr_lower_with_small_sample_than_large() -> None:
    """같은 통계인데 표본만 작으면 PSR 더 낮아야 (n 가 분자에 sqrt 로 들어감)."""
    rng = np.random.default_rng(seed=7)
    rets = rng.normal(loc=0.003, scale=0.01, size=2000)
    psr_big = probabilistic_sharpe_ratio(rets[:30])
    psr_small = probabilistic_sharpe_ratio(rets)
    assert psr_big < psr_small


def test_psr_lower_with_negative_skewness() -> None:
    """음의 왜도 (좌측 꼬리 두꺼움) → 같은 SR 보다 PSR 더 낮음."""
    rng = np.random.default_rng(seed=10)
    base = rng.normal(loc=0.003, scale=0.01, size=500)
    # 가장 큰 양수 몇 개를 큰 음수로 바꿔 왜도 음수로 비틂. SR 은 살짝 낮아지지만 음의 왜도가 PSR 에 큰 영향.
    skewed = base.copy()
    skewed[np.argsort(base)[-10:]] = -0.05  # 큰 양수 → 큰 음수
    psr_symmetric = probabilistic_sharpe_ratio(base)
    psr_skewed = probabilistic_sharpe_ratio(skewed)
    assert psr_skewed < psr_symmetric


# ---- benchmark_sr -----------------------------------------------------------


def test_psr_decreases_when_benchmark_raised() -> None:
    """벤치마크 SR 높일수록 PSR 낮아져야."""
    rng = np.random.default_rng(seed=3)
    rets = rng.normal(loc=0.002, scale=0.01, size=500)
    psr_zero = probabilistic_sharpe_ratio(rets, benchmark_sr=0.0)
    psr_higher = probabilistic_sharpe_ratio(rets, benchmark_sr=0.1)
    assert psr_higher < psr_zero


# ---- 결합: PerformanceReport ------------------------------------------------


class NoopStrategy(Strategy):
    def on_bar(self, _bar: Bar) -> list[Signal]:
        return []


def _equity_curve(rets: list[float], *, base: float = 1_000_000.0) -> pd.Series:
    eq = [base]
    for r in rets:
        eq.append(eq[-1] * (1 + r))
    idx = [datetime(2026, 1, 1) + timedelta(days=i) for i in range(len(eq))]
    return pd.Series(eq, index=pd.DatetimeIndex(idx, name="ts"), name="equity")


def test_performance_report_includes_psr() -> None:
    """compute() 가 PerformanceReport.probabilistic_sharpe 를 채운다."""
    rng = np.random.default_rng(seed=42)
    rets = rng.normal(loc=0.005, scale=0.01, size=200).tolist()
    eq = _equity_curve(rets)
    report = compute(eq, trades=[], initial_equity=1_000_000.0)
    assert 0.0 <= report.probabilistic_sharpe <= 1.0
    # SR 양수가 명확하면 0.5 보다 위.
    assert report.probabilistic_sharpe > 0.5


def test_performance_report_as_dict_exposes_psr() -> None:
    report = compute(
        _equity_curve([0.01, -0.005, 0.012, -0.003, 0.008]),
        trades=[],
        initial_equity=1_000_000.0,
    )
    d = report.as_dict()
    assert "probabilistic_sharpe" in d


def test_backtester_run_propagates_psr() -> None:
    """Backtester.run() → BacktestResult.report.probabilistic_sharpe 자동 채움."""
    rows = []
    rng = np.random.default_rng(seed=5)
    rets = rng.normal(loc=0.001, scale=0.005, size=50)
    price = 100.0
    for i, r in enumerate(rets):
        price *= 1 + r
        rows.append(
            {
                "ts": datetime(2026, 1, 1) + timedelta(days=i),
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": 1000,
            }
        )
    df = pd.DataFrame(rows).set_index("ts")
    result = Backtester(strategy=NoopStrategy(), initial_cash=1_000_000.0).run("X", df)
    # noop 전략이라 trades 0, sharpe = 0 (equity_curve 가 모두 1_000_000). PSR = 0.0.
    assert result.report.probabilistic_sharpe == 0.0
