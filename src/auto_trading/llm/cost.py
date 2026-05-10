"""LLM 호출 wiring 헬퍼 — cost_tracker 와 호출자(news/notify/strategy) 사이 어댑터.

29단계 PR15 (#16 2차) — PR13 의 ``CostTracker`` 모듈을 실제 LLM 호출 지점에
연결한다. 사용 패턴:

    tracker = get_default_tracker()
    check_budget_or_raise(tracker)              # 호출 직전 — over 면 raise
    response = client.messages.create(...)
    record_response(tracker, response, model=model)  # 호출 직후 — JSONL append

알림(텔레그램)은 *이 모듈에서 안 함* — preflight CLI 명령 또는 별도 cron 이
budget_status 를 보고 결정. 호출 핫패스에 알림 의존성을 묶지 않는 게 핵심.

테스트는 단위 함수별로 격리. mock anthropic 응답으로 wiring 검증은 호출자
(news/proposal, notify/insight, strategy/llm_advisor) 단위 테스트에서.
"""

from __future__ import annotations

from typing import Any

import structlog

from ..config import Settings, get_settings
from .cost_tracker import BudgetStatus, CostEntry, CostTracker

log = structlog.get_logger(__name__)

__all__ = [
    "LLMBudgetExceeded",
    "check_budget_or_raise",
    "get_default_tracker",
    "record_response",
]


class LLMBudgetExceeded(Exception):
    """월 한도 초과로 LLM 호출이 차단된 상황. 호출자가 catch 해서 안내 메시지로 변환."""

    def __init__(self, status: BudgetStatus) -> None:
        super().__init__(
            f"LLM 월 한도 초과: ${status.spent_usd:.2f} / ${status.budget_usd:.2f} "
            f"({status.pct_used:.0%}, {status.year_month})"
        )
        self.status = status


def get_default_tracker(settings: Settings | None = None) -> CostTracker | None:
    """``Settings.llm_cost_path`` 기반 default 트래커. 미설정 시 None (비활성)."""
    s = settings or get_settings()
    if s.llm_cost_path is None:
        return None
    return CostTracker(
        path=s.llm_cost_path,
        monthly_budget_usd=s.llm_monthly_budget_usd,
    )


def check_budget_or_raise(tracker: CostTracker | None) -> None:
    """호출 *직전* 에 호출. 한도 100% 초과면 ``LLMBudgetExceeded`` raise.

    tracker None (비활성) 이면 no-op — 기존 동작 유지.
    """
    if tracker is None:
        return
    status = tracker.budget_status()
    if status.over:
        log.warning(
            "llm.budget_exceeded",
            spent_usd=status.spent_usd,
            budget_usd=status.budget_usd,
            pct_used=status.pct_used,
            year_month=status.year_month,
        )
        raise LLMBudgetExceeded(status)


def record_response(
    tracker: CostTracker | None,
    response: Any,
    *,
    model: str,
) -> CostEntry | None:
    """anthropic ``Message`` 응답 → JSONL append.

    응답 객체에 ``usage.input_tokens`` / ``usage.output_tokens`` 가 없으면
    (mock / 옛 SDK) 조용히 skip — 호출자 동작에 영향 X.
    """
    if tracker is None:
        return None
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    if input_tokens == 0 and output_tokens == 0:
        return None
    return tracker.record(
        model=model,
        input_tokens=int(input_tokens),
        output_tokens=int(output_tokens),
    )
