"""백테스트 결과 비교 — 29단계 PR32 (#23).

두 ``report.json`` (단일 백테스트의 ``PerformanceReport.as_dict()`` 출력) 을 받아
메트릭별 차이 + *회귀* 여부를 산출.

회귀 판정 기준:
- **higher_is_better**: sharpe, probabilistic_sharpe, total_return_pct, cagr_pct, win_rate_pct →
  new < old 면 회귀.
- **less_negative_is_better**: max_drawdown_pct (음수) → new < old 면 회귀 (더 깊은 손실).
- **lower_is_better**: annualized_vol_pct → new > old 면 회귀.
- **info_only**: num_trades, num_round_trips, initial_equity, final_equity → 회귀 판정 X
  (단순 표시).

설계 원칙:
- 순수 함수 — 외부 호출 0. 파일 IO 는 호출자(CLI) 책임.
- "어느 쪽이 더 나은지" 만 판정 — *임계값* (예: "Sharpe -0.1 이상 떨어지면 경고") 는
  의도적으로 두지 않음. PR-by-PR 회귀 감지가 목적이라 미세 변화도 보이게.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "Direction",
    "MetricDiff",
    "compare_reports",
]


class Direction(StrEnum):
    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"
    LESS_NEGATIVE_IS_BETTER = "less_negative_is_better"
    INFO_ONLY = "info_only"


# report.json 키 → 방향. 향후 새 메트릭 추가 시 여기만 갱신.
_DIRECTION: dict[str, Direction] = {
    "sharpe": Direction.HIGHER_IS_BETTER,
    "probabilistic_sharpe": Direction.HIGHER_IS_BETTER,
    "total_return_pct": Direction.HIGHER_IS_BETTER,
    "cagr_pct": Direction.HIGHER_IS_BETTER,
    "win_rate_pct": Direction.HIGHER_IS_BETTER,
    "max_drawdown_pct": Direction.LESS_NEGATIVE_IS_BETTER,
    "annualized_vol_pct": Direction.LOWER_IS_BETTER,
    "initial_equity": Direction.INFO_ONLY,
    "final_equity": Direction.INFO_ONLY,
    "num_trades": Direction.INFO_ONLY,
    "num_round_trips": Direction.INFO_ONLY,
}


@dataclass(frozen=True)
class MetricDiff:
    """한 메트릭의 old/new/delta + 회귀 여부."""

    name: str
    old: float
    new: float
    delta: float  # new - old
    direction: Direction
    regression: bool  # 방향 기준으로 new 가 더 나쁘면 True


def _is_regression(old: float, new: float, direction: Direction) -> bool:
    """new 가 old 보다 *더 나쁜지*. delta=0 이면 회귀 아님."""
    if direction is Direction.INFO_ONLY or old == new:
        return False
    if direction is Direction.LOWER_IS_BETTER:
        return new > old
    # HIGHER_IS_BETTER / LESS_NEGATIVE_IS_BETTER 모두 "new < old → 회귀" 동일.
    return new < old


def compare_reports(old: dict, new: dict) -> list[MetricDiff]:
    """두 ``report.json`` dict → 메트릭별 MetricDiff 리스트.

    출력 순서는 ``_DIRECTION`` 선언 순서 (사람이 읽기 좋은 우선순위).
    한쪽에만 있는 키는 다른 쪽 0.0 으로 취급. 알 수 없는 키(미래 추가 메트릭) 는
    INFO_ONLY 로 분류.
    """
    diffs: list[MetricDiff] = []
    seen_keys: set[str] = set()
    # 알려진 키 우선 (선언 순서 보존).
    for key, direction in _DIRECTION.items():
        if key not in old and key not in new:
            continue
        old_val = float(old.get(key, 0.0))
        new_val = float(new.get(key, 0.0))
        diffs.append(
            MetricDiff(
                name=key,
                old=old_val,
                new=new_val,
                delta=new_val - old_val,
                direction=direction,
                regression=_is_regression(old_val, new_val, direction),
            )
        )
        seen_keys.add(key)
    # 알 수 없는 키 — INFO_ONLY 로 끝에 추가 (양쪽 모두 숫자일 때만).
    extra = sorted((set(old) | set(new)) - seen_keys)
    for key in extra:
        try:
            old_val = float(old.get(key, 0.0))
            new_val = float(new.get(key, 0.0))
        except TypeError, ValueError:
            continue
        diffs.append(
            MetricDiff(
                name=key,
                old=old_val,
                new=new_val,
                delta=new_val - old_val,
                direction=Direction.INFO_ONLY,
                regression=False,
            )
        )
    return diffs
