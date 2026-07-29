"""24단계 — 주간 뉴스 수집·LLM 분석·다음주 day-trade 프로파일 자동 갱신.

Phase A: news/collector.py — RSS 기반 뉴스 수집 + JSON 캐시.
Phase B: news/proposal.py — LLM 강제 도구로 DaytradeProfile 변경 제안 (예정).
Phase C: news/profile_writer.py — 제안을 daytrade-current.yaml 로 자동 적용 (예정).
"""

from .collector import NewsItem, collect_recent_news, parse_feed
from .profile_writer import (
    ApplyResult,
    SafetyCaps,
    apply_and_persist,
    apply_proposal,
    format_apply_message,
)
from .proposal import (
    DaytradeProposal,
    propose_daytrade_profile,
)

__all__ = [
    "ApplyResult",
    "DaytradeProposal",
    "NewsItem",
    "SafetyCaps",
    "apply_and_persist",
    "apply_proposal",
    "collect_recent_news",
    "format_apply_message",
    "parse_feed",
    "propose_daytrade_profile",
]
