"""Look-ahead bias 자동 감지 — 29단계 PR33 (#18).

무엇/왜:
- backtest/CLAUDE.md 는 "전략은 bar[i] 까지만 본다" 를 명시하지만 코드 리뷰만으로는
  ``pandas.rolling(center=True)``, ``iloc[i+1]``, 미리 fetch 된 daily bars 참조 등을
  놓치기 쉬움. CI 자동 검출이 필요.
- *데이터 perturbation* 방식 회귀:
  1. 원본 df 로 백테스트 → 에쿼티 시계열 A
  2. df 의 perturb_from 이후 가격을 x100 으로 변조한 df' 로 백테스트 → 에쿼티 시계열 B
  3. cutoff = df.index[perturb_from]. A 와 B 의 ``ts < cutoff`` 시점 에쿼티가 *완전히
     동일* 하면 전략이 미래 정보를 보지 않은 것. 다르면 look-ahead 의심.

설계 결정:
- 변조 방법: 종가·시고저 x 100 (절대 차이 크게 → 미세 누설도 검출).
- 비교 지표: ``equity_curve`` 만 — trade.price 는 다음 봉 시가에 체결되므로 perturb 경계
  bar 에서 일부러 다를 수 있어 노이즈. 에쿼티 (해당 bar 종가 기준) 가 깔끔.
- 외부 호출 0. assertion 헬퍼만 — 운영 코드 경로 변경 없음.

어떻게 확인: ``tests/test_backtest_lookahead.py`` 의 등록 전략 parametrize + cheater
strategy fail 케이스.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd

from ..strategy.base import Strategy
from .engine import Backtester

__all__ = ["LookaheadDetected", "assert_no_lookahead"]


class LookaheadDetected(AssertionError):
    """전략이 미래 데이터를 본 것으로 의심되는 시점에서 발생."""


def assert_no_lookahead(
    strategy_factory: Callable[[], Strategy],
    df: pd.DataFrame,
    *,
    perturb_from: int | None = None,
    symbol: str = "TEST",
    initial_cash: float = 10_000_000.0,
    rtol: float = 1e-9,
) -> None:
    """등록 전략이 미래 데이터를 참조하는지 자동 감지.

    raises ``LookaheadDetected`` if 변조 전 에쿼티 시계열이 두 실행 사이에서 다름.

    매개변수:
    - ``perturb_from``: 변조 시작 인덱스. None 이면 ``len(df) // 2``.
    - ``rtol``: float 비교 상대오차 — 산술 노이즈 허용 (보통 0 으로 일치해야 함).
    """
    n = len(df)
    if n < 4:
        raise ValueError(f"df 길이 부족 (look-ahead 검출 의미 X): {n}")
    if perturb_from is None:
        perturb_from = n // 2
    if not (0 < perturb_from < n):
        raise ValueError(f"perturb_from out of range: {perturb_from}")

    # Run 1: 원본
    bt1 = Backtester(strategy=strategy_factory(), initial_cash=initial_cash)
    r1 = bt1.run(symbol, df)

    # Run 2: 변조
    df2 = df.copy()
    cols = ["open", "high", "low", "close"]
    cidx = df2.columns.get_indexer(cols)
    df2.iloc[perturb_from:, cidx] = df2.iloc[perturb_from:, cidx] * 100.0
    bt2 = Backtester(strategy=strategy_factory(), initial_cash=initial_cash)
    r2 = bt2.run(symbol, df2)

    cutoff_ts = df.index[perturb_from]
    eq1 = r1.equity_curve[r1.equity_curve.index < cutoff_ts]
    eq2 = r2.equity_curve[r2.equity_curve.index < cutoff_ts]
    if len(eq1) != len(eq2):
        raise LookaheadDetected(
            f"look-ahead 의심 — 변조 전 에쿼티 길이 불일치: {len(eq1)} vs {len(eq2)}"
        )
    if len(eq1) == 0:
        # perturb_from=1 같은 극단 — 비교 대상 없음.
        return
    a = eq1.to_numpy()
    b = eq2.to_numpy()
    diff = np.abs(a - b)
    tol = rtol * np.maximum(np.abs(a), 1.0)
    if np.any(diff > tol):
        i = int(np.argmax(diff))
        raise LookaheadDetected(
            f"look-ahead 의심 — 변조 전 시점 {eq1.index[i]} 에쿼티 불일치: "
            f"{a[i]:.6f} vs {b[i]:.6f} (diff={diff[i]:.6f})"
        )
