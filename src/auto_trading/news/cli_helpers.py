"""weekly-proposal CLI 의 도메인 헬퍼.

cli.py 의 `weekly-proposal` 명령에서만 호출되는 단계별 함수.
- `should_skip_weekly_proposal_for_holiday` — 토요일이 직접 휴장이거나 다음 거래일까지
  5일 이상이면 LLM 비용 절약을 위해 시동 생략 결정.
- `WeeklyInputs` — reports + news 묶음 dataclass (`is_empty` 프로퍼티 포함).
- `run_weekly_proposal_cycle` — propose_daytrade_profile (LLM) → apply_and_persist →
  format_apply_message 를 한 번에.

여기에 typer 의존을 두지 않는다 — 콘솔 출력·typer.Exit 은 cli.py 책임.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import structlog

from ..market import calendar as krx_calendar
from ..reports.daily import DailyReport
from .collector import NewsItem
from .profile_writer import ApplyResult, apply_and_persist, format_apply_message
from .proposal import DaytradeProposal, propose_daytrade_profile

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class WeeklyInputs:
    """LLM 입력 데이터셋 — reports + news 합본."""

    reports: list[DailyReport]
    news: list[NewsItem]

    @property
    def is_empty(self) -> bool:
        return not self.reports and not self.news


def should_skip_weekly_proposal_for_holiday(
    *,
    force: bool,
    today: date,
) -> str | None:
    """오늘이 휴장 + 다음 거래일까지 5일 이상이면 스킵 사유 메시지 반환. 그 외 None.

    `force=True` 면 무조건 None (수동 디버그용). 토요일 cron 의 LLM 비용 폭주 방지.
    """
    if force:
        return None
    next_day = krx_calendar.next_trading_day(today)
    gap_days = (next_day - today).days
    if not krx_calendar.is_trading_day(today) and gap_days >= 5:
        log.info(
            "runner.holiday_skip",
            command="weekly-proposal",
            date=today.isoformat(),
            gap_days=gap_days,
        )
        return (
            f"오늘({today.isoformat()}) 기준 다음 거래일까지 {gap_days}일 — "
            "weekly-proposal 시동 생략 (LLM 비용 절약)."
        )
    return None


def run_weekly_proposal_cycle(
    inputs: WeeklyInputs,
    *,
    current_path: Path,
    fallback_path: Path,
    target_path: Path | None,
    client: Any,  # anthropic.Anthropic — 호출자가 _build_anthropic_client 로 주입
    model: str,
    max_news_items: int,
) -> tuple[DaytradeProposal, ApplyResult, str]:
    """LLM 호출 → 안전 캡 적용 → 콘솔/텔레그램용 한국어 메시지까지.

    target_path 가 None 이면 current_path 를 그대로 덮어쓰기.
    """
    proposal = propose_daytrade_profile(
        inputs.reports,
        inputs.news,
        client=client,
        model=model,
        max_news_items=max_news_items,
    )
    result = apply_and_persist(
        proposal,
        current_path=current_path,
        target_path=target_path or current_path,
        fallback_path=fallback_path,
    )
    msg = format_apply_message(result, proposal)
    return proposal, result, msg


__all__ = [
    "WeeklyInputs",
    "run_weekly_proposal_cycle",
    "should_skip_weekly_proposal_for_holiday",
]
