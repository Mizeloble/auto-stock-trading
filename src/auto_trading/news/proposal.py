"""24단계 Phase B — 주간 LLM 에 뉴스 입력 + 다음주 day-trade 프로파일 변경 제안.

`notify/insight.py` 의 주간 인사이트와 별개:
- 인사이트는 "지난 주 결과 요약" → 사람이 읽음.
- proposal 은 "다음 주 day-trade 프로파일 어떻게 바꿀지" → Phase C 가 YAML 자동 갱신.

LLM 호출은 주 1회 한정 ([feedback_llm_cost](memory)). 토요일 cron 에서 한 번 호출.

응답은 pydantic `DaytradeProposal` 로 검증 — 자유 텍스트 차단, propose_daytrade_profile
도구 강제. 모든 변경 필드는 옵션(None) — LLM 이 "유지" 의미로 None 응답 가능.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from ..llm import extract_tool_use_input
from ..llm.cost import check_budget_or_raise, get_default_tracker, record_response
from ..reports.daily import DailyReport
from .collector import NewsItem


class DaytradeProposal(BaseModel):
    """LLM 응답 검증. 모든 변경 필드는 옵션 — None 이면 "유지"."""

    summary_ko: str = Field(min_length=1, max_length=300)
    rationale_ko: str = Field(min_length=1, max_length=800)
    confidence: float = Field(ge=0.0, le=1.0)

    # 변경 제안 — 23단계 Phase A~G 의 RiskLimits·params 와 1:1 매핑.
    top_n: int | None = Field(default=None, ge=1, le=10)
    cooldown_min: int | None = Field(default=None, ge=0, le=240)
    momentum_lookback_min: int | None = Field(default=None, ge=1, le=60)
    momentum_threshold_pct: float | None = Field(default=None, ge=0.0, le=10.0)
    stop_loss_pct: float | None = Field(default=None, ge=0.0, le=0.10)
    trailing_stop_pct: float | None = Field(default=None, ge=0.0, le=0.10)
    partial_take_pct: float | None = Field(default=None, ge=0.0, le=0.10)
    partial_take_qty_ratio: float | None = Field(default=None, ge=0.0, le=1.0)
    daily_loss_kill: bool | None = None

    themes_to_watch: list[str] = Field(default_factory=list, max_length=10)
    symbols_to_avoid: list[str] = Field(default_factory=list, max_length=20)


class _AnthropicLike(Protocol):
    @property
    def messages(self) -> Any: ...


SYSTEM_PROMPT_KO = """\
당신은 한국 주식 day-trade 자동매매 봇의 주간 운영 분석가입니다.
지난 주의 (a) 일일 트레이딩 결과 (b) 시장 뉴스 헤드라인 을 보고, 다음 주에 적용할 day-trade
프로파일의 파라미터를 propose_daytrade_profile 도구로 제안하세요.

조정 가능한 파라미터:
- top_n: 1~10 (거래량 상위 후보 수)
- cooldown_min: 0~240 (재진입 대기 분)
- momentum_lookback_min: 1~60 (지수 일중 모멘텀 lookback 분)
- momentum_threshold_pct: 0.0~10.0 (% 단위, 0=게이트 비활성)
- stop_loss_pct: 0.0~0.10 (예: 0.01 = -1% 손절)
- trailing_stop_pct: 0.0~0.10 (예: 0.005 = -0.5% 트레일링)
- partial_take_pct: 0.0~0.10 (예: 0.015 = +1.5% 부분 익절)
- partial_take_qty_ratio: 0.0~1.0 (부분 익절 비율, 보통 0.5)
- daily_loss_kill: bool (일일 -3% 손실 시 강제 청산)

규칙:
- 변경할 필드만 채우고, 유지할 필드는 비워두세요(필드 자체를 응답에 포함하지 말 것).
- summary_ko: 한국어 1~3문장 — 다음 주 핵심 변경.
- rationale_ko: 한국어 4~10문장 — 데이터·뉴스 근거.
- themes_to_watch: 다음 주 주목할 섹터/테마 (최대 10).
- symbols_to_avoid: 다음 주 피할 종목 코드 (최대 20).
- confidence: 0.0~1.0, 데이터 충실도·일관성 기반.
- 추가 텍스트 응답 없이 propose_daytrade_profile 도구 호출만 하세요.
"""

PROPOSE_TOOL = {
    "name": "propose_daytrade_profile",
    "description": "다음 주 day-trade 프로파일의 파라미터 변경 제안.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary_ko": {"type": "string", "minLength": 1, "maxLength": 300},
            "rationale_ko": {"type": "string", "minLength": 1, "maxLength": 800},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "top_n": {"type": "integer", "minimum": 1, "maximum": 10},
            "cooldown_min": {"type": "integer", "minimum": 0, "maximum": 240},
            "momentum_lookback_min": {"type": "integer", "minimum": 1, "maximum": 60},
            "momentum_threshold_pct": {"type": "number", "minimum": 0.0, "maximum": 10.0},
            "stop_loss_pct": {"type": "number", "minimum": 0.0, "maximum": 0.10},
            "trailing_stop_pct": {"type": "number", "minimum": 0.0, "maximum": 0.10},
            "partial_take_pct": {"type": "number", "minimum": 0.0, "maximum": 0.10},
            "partial_take_qty_ratio": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "daily_loss_kill": {"type": "boolean"},
            "themes_to_watch": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 10,
            },
            "symbols_to_avoid": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 20,
            },
        },
        "required": ["summary_ko", "rationale_ko", "confidence"],
    },
}


# ---- 입력 요약 -----------------------------------------------------------


def _summarize_reports(reports: Iterable[DailyReport]) -> dict[str, Any]:
    items = list(reports)
    if not items:
        return {"days": 0}
    buys = sum(len(r.buys) for r in items)
    liqs = sum(len(r.liquidations) for r in items)
    err = sum(r.error_count for r in items)
    pnls = [r.realized_pnl for r in items if r.realized_pnl is not None]
    pnls_after_cost = [
        r.realized_pnl_after_cost for r in items if r.realized_pnl_after_cost is not None
    ]
    chosen_freq: dict[str, int] = {}
    for r in items:
        for sym in r.discovery_chosen:
            chosen_freq[sym] = chosen_freq.get(sym, 0) + 1
    return {
        "days": len(items),
        "date_range": [items[0].date, items[-1].date],
        "buys_total": buys,
        "liquidations_total": liqs,
        "error_total": err,
        "realized_pnl": {
            "samples": len(pnls),
            "sum": sum(pnls) if pnls else None,
            "avg": (sum(pnls) / len(pnls)) if pnls else None,
        },
        "realized_pnl_after_cost": {
            "samples": len(pnls_after_cost),
            "sum": sum(pnls_after_cost) if pnls_after_cost else None,
            "avg": (sum(pnls_after_cost) / len(pnls_after_cost) if pnls_after_cost else None),
        },
        "chosen_freq_top": sorted(chosen_freq.items(), key=lambda kv: -kv[1])[:8],
    }


def _summarize_news(news: Iterable[NewsItem], *, max_items: int = 80) -> list[dict[str, Any]]:
    """헤드라인 + 본문 발췌 — 토큰 절약. 본문은 collector 에서 400자 truncate.

    default 80 (이전 30) — 6개 RSS 소스 합산 ~200건 중 상위 80건을 LLM 입력으로.
    호출자(`weekly-proposal --max-news-items`) 가 override 가능. 토큰/비용 영향은
    proposal.py SYSTEM_PROMPT 와 README 참고 (호출당 ~$0.45 / 월 ~$2 추정).
    """
    return [
        {
            "title": it.title,
            "summary": it.summary,
            "source": it.source,
            "tickers": list(it.tickers),
            "date": it.published_at.date().isoformat(),
        }
        for it in list(news)[:max_items]
    ]


def _summarize_current_profile(profile_dict: dict[str, Any] | None) -> dict[str, Any]:
    """현재 프로파일 핵심 부분만 — LLM 이 "이전 값 → 새 값" 비교에 활용."""
    if not profile_dict:
        return {}
    risk = profile_dict.get("risk") or {}
    params = profile_dict.get("params") or {}
    return {
        "strategy": profile_dict.get("strategy"),
        "top_n": profile_dict.get("top_n"),
        "params": {
            "cooldown_min": params.get("cooldown_min"),
            "momentum_lookback_min": params.get("momentum_lookback_min"),
            "momentum_threshold_pct": params.get("momentum_threshold_pct"),
        },
        "risk": {
            "stop_loss_pct": risk.get("stop_loss_pct"),
            "trailing_stop_pct": risk.get("trailing_stop_pct"),
            "partial_take_pct": risk.get("partial_take_pct"),
            "partial_take_qty_ratio": risk.get("partial_take_qty_ratio"),
            "daily_loss_kill": risk.get("daily_loss_kill"),
        },
    }


# ---- LLM 호출 ------------------------------------------------------------


def propose_daytrade_profile(
    reports: list[DailyReport],
    news: list[NewsItem],
    *,
    client: _AnthropicLike,
    current_profile: dict[str, Any] | None = None,
    model: str = "claude-opus-4-7",
    max_tokens: int = 1024,
    max_news_items: int = 80,
) -> DaytradeProposal:
    """LLM 호출 → DaytradeProposal. 검증 실패 시 ValidationError raise.

    `max_news_items` — 수집된 뉴스 중 LLM 입력으로 보낼 상한. default 80
    (호출당 input ~25k tok ≈ $0.45, 주 1회 cron 기준 월 ~$2).
    """
    payload = {
        "reports": _summarize_reports(reports),
        "news": _summarize_news(news, max_items=max_news_items),
        "current_profile": _summarize_current_profile(current_profile),
    }
    # 29단계 PR15 — 호출 직전 한도 검사 + 호출 직후 record. 미활성(None) 시 no-op.
    tracker = get_default_tracker()
    check_budget_or_raise(tracker)
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT_KO,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        tools=[PROPOSE_TOOL],
        tool_choice={"type": "tool", "name": "propose_daytrade_profile"},
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "다음 데이터를 보고 propose_daytrade_profile 도구를 호출해 "
                            f"다음 주 프로파일 변경을 제안하세요:\n{payload}"
                        ),
                    }
                ],
            }
        ],
    )
    record_response(tracker, response, model=model)
    tool_input = extract_tool_use_input(response, "propose_daytrade_profile")
    return DaytradeProposal.model_validate(tool_input)


# ---- 포맷터 -------------------------------------------------------------


def format_proposal_message(proposal: DaytradeProposal) -> str:
    """텔레그램 / CLI 출력용 한국어 메시지 — Phase C 가 YAML 갱신 후 같이 푸시."""
    lines = ["🗞 다음 주 day-trade 프로파일 제안", "", proposal.summary_ko]
    changes: list[tuple[str, Any]] = []
    for k in (
        "top_n",
        "cooldown_min",
        "momentum_lookback_min",
        "momentum_threshold_pct",
        "stop_loss_pct",
        "trailing_stop_pct",
        "partial_take_pct",
        "partial_take_qty_ratio",
        "daily_loss_kill",
    ):
        v = getattr(proposal, k)
        if v is not None:
            changes.append((k, v))
    if changes:
        lines.append("")
        lines.append("📐 변경 파라미터")
        for k, v in changes:
            lines.append(f"• {k} → {v}")
    if proposal.themes_to_watch:
        lines.append("")
        lines.append("📡 주목 테마: " + ", ".join(proposal.themes_to_watch))
    if proposal.symbols_to_avoid:
        lines.append("⛔ 회피 종목: " + ", ".join(proposal.symbols_to_avoid))
    lines.append("")
    lines.append("📝 근거")
    lines.append(proposal.rationale_ko)
    lines.append("")
    lines.append(f"신뢰도: {proposal.confidence:.2f}")
    return "\n".join(lines)


def build_proposal_callback(
    client: _AnthropicLike,
    *,
    days_reports: int = 7,
    days_news: int = 7,
    reports_root: Path = Path("data/reports"),
    news_root: Path = Path("data/news"),
    current_profile: dict[str, Any] | None = None,
    model: str = "claude-opus-4-7",
) -> Callable[[], Awaitable[DaytradeProposal | None]]:
    """주간 cron 또는 수동 호출용 비동기 콜백.

    - reports/news 둘 다 비면 None 반환 — 호출자가 "데이터 부족"으로 처리.
    - 동기 SDK 호출은 `asyncio.to_thread` 로 별도 스레드.
    """
    from ..notify.insight import collect_recent_reports
    from .collector import collect_recent_news

    async def _runner() -> DaytradeProposal | None:
        reports = collect_recent_reports(root=reports_root, days=days_reports)
        news = collect_recent_news(days=days_news, cache_root=news_root)
        if not reports and not news:
            return None
        return await asyncio.to_thread(
            propose_daytrade_profile,
            reports,
            news,
            client=client,
            current_profile=current_profile,
            model=model,
        )

    return _runner


__all__ = [
    "PROPOSE_TOOL",
    "SYSTEM_PROMPT_KO",
    "DaytradeProposal",
    "build_proposal_callback",
    "format_proposal_message",
    "propose_daytrade_profile",
]
