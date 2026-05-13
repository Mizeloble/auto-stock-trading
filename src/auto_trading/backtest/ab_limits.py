"""백테스트 A/B 비교 프레임워크 — 29단계 PR41 (#8).

무엇/왜:
- 같은 시그널 셋을 *두 Backtester 설정* (보수 vs 공격) 으로 시뮬 → 메트릭 차이 비교.
- "이 한도/슬리피지/수수료 변형이 PnL·MDD 에 어떻게 영향" 을 직접 답함.

현재 ``Backtester`` 가 ``RiskLimits`` 를 직접 받지 않아 plan #8 의 *RiskLimits 변형*
은 후속 PR (Backtester-RiskManager 통합) 후에 자연 결합. 지금은 의미 있는 Backtester
kwargs (slippage_bps / commission_rate / position_sizing / atr_risk_pct 등) 변형 비교.

설계 결정:
- 두 변형은 각각 *독립* Backtester (broker state 공유 X).
- 결과: ``ABResult`` — 두 보고서 + 메트릭 차이(PR32 ``compare_reports`` 재사용).
- 외부 호출 0. CLI 결합은 별도 PR.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pandas as pd

from ..strategy.base import Strategy
from .compare import MetricDiff, compare_reports
from .engine import Backtester
from .metrics import PerformanceReport

__all__ = ["ABResult", "ab_compare_backtests"]


@dataclass(frozen=True)
class ABResult:
    """두 Backtester 설정의 결과 + 메트릭 차이."""

    label_a: str
    label_b: str
    report_a: PerformanceReport
    report_b: PerformanceReport
    diffs: list[MetricDiff]  # PR32 compare_reports — a→b 차이


def ab_compare_backtests(
    strategy_factory: Callable[[], Strategy],
    symbol: str,
    df: pd.DataFrame,
    *,
    variant_a: dict[str, Any],
    variant_b: dict[str, Any],
    label_a: str = "conservative",
    label_b: str = "aggressive",
) -> ABResult:
    """동일 데이터·전략으로 두 Backtester 변형 실행 후 비교.

    ``variant_a`` / ``variant_b`` 는 ``Backtester`` 의 kwargs dict. ``strategy`` 와
    ``initial_cash`` 같은 공통 인자는 이 dict 에 넣음. ``strategy`` 는 factory 가 매번
    새 인스턴스 생성하므로 직접 넣지 말 것 — kwargs 에 ``strategy`` 가 있으면 무시.

    예:
    ```python
    result = ab_compare_backtests(
        strategy_factory, "005930", df,
        variant_a={"slippage_bps": 5.0, "slippage_model": "fixed"},
        variant_b={"slippage_bps": 5.0, "slippage_model": "impact", "impact_coefficient": 3.0},
        label_a="fixed_slip",
        label_b="impact_slip",
    )
    for d in result.diffs:
        print(d.name, d.delta, "regression" if d.regression else "ok")
    ```
    """

    def _run(variant: dict[str, Any]) -> PerformanceReport:
        kwargs = {k: v for k, v in variant.items() if k != "strategy"}
        bt = Backtester(strategy=strategy_factory(), **kwargs)
        return bt.run(symbol, df).report

    report_a = _run(variant_a)
    report_b = _run(variant_b)
    diffs = compare_reports(report_a.as_dict(), report_b.as_dict())
    return ABResult(
        label_a=label_a,
        label_b=label_b,
        report_a=report_a,
        report_b=report_b,
        diffs=diffs,
    )
