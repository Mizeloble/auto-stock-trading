"""주간 인사이트 — 직전 N일치 일일 리포트를 LLM 으로 누적 분석.

22단계 Phase B. 호출 빈도는 두 가지만 허용 (메모리 `feedback_llm_cost.md`):
1. 사람이 명시 트리거 — `auto-trade insight` CLI 또는 텔레그램 `/insight` 명령
2. 사전 합의된 저빈도 cron — 매주 토 09:00 launchd

설계:
- Phase A 의 `data/reports/daily-*.json` 을 입력 데이터셋으로 사용. LLM 에 풀 JSON 대신
  요약 dict 만 보내 토큰 절약.
- 응답은 pydantic `InsightResponse` 로 검증 — `tool_use` 강제로 자유 텍스트 응답 차단.
- system 프롬프트에 `cache_control: ephemeral` — 동일 day 에 두 번 호출 시 캐시 히트.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from ..llm import extract_tool_use_input
from ..llm.cost import check_budget_or_raise, get_default_tracker, record_response
from ..reports.daily import DailyReport, load_daily_report, merge_symbol_names


class InsightResponse(BaseModel):
    """LLM 응답 검증 모델 — `tool_use` input dict 가 이 구조를 충족해야 함."""

    summary_ko: str = Field(min_length=1, max_length=500)
    key_findings: list[str] = Field(default_factory=list, max_length=8)
    recommendations: list[str] = Field(default_factory=list, max_length=6)
    confidence: float = Field(ge=0.0, le=1.0)


class _AnthropicLike(Protocol):
    """anthropic.Anthropic 의 messages.create 호출만 사용 — DI 로 stub 가능."""

    @property
    def messages(self) -> Any: ...


SYSTEM_PROMPT_KO = """\
당신은 한국 주식 day-trade 자동매매 봇의 운영 분석가입니다.
직전 N일치 일일 리포트를 받아 누적 패턴·이상치·전략 조정 권고를 propose_insight 도구로 제안하세요.

규칙:
- summary_ko: 한국어 2~4문장. 그 주의 핵심 흐름(매매 빈도·승률·시장 게이트 통과 비율 등) 요약.
- key_findings: 한국어 짧은 문장 3~6개. 데이터 기반의 패턴/이상치만(예: "5일 중 3일 게이트 차단", "특정 종목이 N회 연속 발굴", "에러 카운트 급증").
- recommendations: 한국어 짧은 문장 1~4개. 구체적인 운영 변경 제안(예: "top_n 을 3 → 2 로 낮춰 매수 가중 키우기", "게이트 SMA 20 → 15 단축 검토"). 없으면 빈 리스트.
- 종목을 언급할 때는 항상 "코드 종목명" 형식(예: "005930 삼성전자")으로 쓰세요. 입력의 symbol_names 와 chosen_freq_top.name 을 참고. 이름이 없는 코드만 코드 단독 표기 허용.
- confidence: 0.0~1.0, 데이터 충분도·일관성에 따른 본인 확신도.
- 추가 텍스트 응답 없이 propose_insight 도구 호출 결과만 내세요.
"""

PROPOSE_INSIGHT_TOOL = {
    "name": "propose_insight",
    "description": "주간 누적 분석 결과를 한 번만 제안한다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary_ko": {"type": "string", "minLength": 1, "maxLength": 500},
            "key_findings": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 8,
            },
            "recommendations": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 6,
            },
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        },
        "required": ["summary_ko", "key_findings", "recommendations", "confidence"],
    },
}


# ---- 데이터 수집 ---------------------------------------------------------


def collect_recent_reports(
    *,
    root: Path | str = "data/reports",
    days: int = 7,
) -> list[DailyReport]:
    """`<root>/daily-*.json` 중 최신 N개를 날짜 오름차순으로 반환.

    파일이 적으면 가용한 만큼만 — 빈 리스트면 호출자가 "데이터 부족" 처리.
    """
    base = Path(root)
    if not base.exists():
        return []
    files = sorted(base.glob("daily-*.json"))
    if not files:
        return []
    selected = files[-days:] if days > 0 else files
    out: list[DailyReport] = []
    for path in selected:
        try:
            out.append(load_daily_report(path))
        except OSError, ValueError:
            continue
    return out


def _summarize_reports(reports: Iterable[DailyReport]) -> dict[str, Any]:
    """LLM 입력용 요약 dict — 풀 JSON 대신 핵심 지표만."""
    items = list(reports)
    days = len(items)
    if days == 0:
        return {"days": 0}

    symbol_names = merge_symbol_names(items)
    chosen_freq: dict[str, int] = {}
    blocked_count = 0
    buys_total = 0
    sells_total = 0
    liqs_total = 0
    err_total = 0
    realized_pnls: list[float] = []
    gate_pass: dict[str, int] = {}
    gate_total: dict[str, int] = {}
    dry_run_days = 0
    # 데이터 기반 전략 수립용 신규 집계 — 승률·보유시간·사유별 성과·국면·미진입·반사실.
    regime_days: dict[str, int] = {}
    breadth_adv: list[float] = []
    breadth_passed_days = 0
    breadth_blocked_days = 0
    no_trade_reasons: dict[str, int] = {}
    all_round_trips: list[Any] = []
    blocked_day_changes: list[float] = []

    for r in items:
        for sym in r.discovery_chosen:
            chosen_freq[sym] = chosen_freq.get(sym, 0) + 1
        blocked_count += len(r.discovery_blocked)
        buys_total += len(r.buys)
        sells_total += len(r.sells)
        liqs_total += len(r.liquidations)
        err_total += r.error_count
        if r.realized_pnl is not None:
            realized_pnls.append(r.realized_pnl)
        if r.dry_run:
            dry_run_days += 1
        for g in r.market_gates:
            gate_total[g.market] = gate_total.get(g.market, 0) + 1
            if g.above:
                gate_pass[g.market] = gate_pass.get(g.market, 0) + 1
        if r.regime:
            regime_days[r.regime] = regime_days.get(r.regime, 0) + 1
        if r.breadth_advancing_pct is not None:
            breadth_adv.append(r.breadth_advancing_pct)
        if r.breadth_passed is True:
            breadth_passed_days += 1
        elif r.breadth_passed is False:
            breadth_blocked_days += 1
        if r.no_trade_reason and not r.buys:
            no_trade_reasons[r.no_trade_reason] = no_trade_reasons.get(r.no_trade_reason, 0) + 1
        all_round_trips.extend(r.round_trips)
        for bo in r.blocked_outcomes:
            if bo.day_change_pct is not None:
                blocked_day_changes.append(bo.day_change_pct)

    return {
        "days": days,
        "date_range": [items[0].date, items[-1].date],
        "modes": list({r.mode for r in items}),
        "dry_run_days": dry_run_days,
        "buys_total": buys_total,
        # 장중 청산(stop/trailing/partial) SELL 체결 수. 2026-05-16 이전엔 리포트가
        # SELL 을 버려 0 으로 보였고, 인사이트가 "청산 회전 낮음" 으로 오판했다.
        "sells_total": sells_total,
        # EOD 강제청산 + 장중 매도 합 — 실제 회전 지표.
        "exits_total": sells_total + liqs_total,
        "liquidations_total": liqs_total,
        "error_total": err_total,
        "blocked_total": blocked_count,
        "symbol_names": symbol_names,
        "chosen_freq_top": [
            {"symbol": sym, "name": symbol_names.get(sym), "days": cnt}
            for sym, cnt in sorted(chosen_freq.items(), key=lambda kv: -kv[1])[:8]
        ],
        "gate_pass_rate": {m: round(gate_pass.get(m, 0) / gate_total[m], 3) for m in gate_total},
        "realized_pnl": {
            "samples": len(realized_pnls),
            "sum": sum(realized_pnls) if realized_pnls else None,
            "avg": (sum(realized_pnls) / len(realized_pnls)) if realized_pnls else None,
            "min": min(realized_pnls) if realized_pnls else None,
            "max": max(realized_pnls) if realized_pnls else None,
        },
        "daily_notes": [{"date": r.date, "note": r.note_ko} for r in items],
        # 데이터 기반 전략 수립 — 의사결정 맥락 집계(LLM 이 패턴을 보게).
        "regime_days": regime_days,
        "breadth": {
            "avg_advancing_pct": (
                round(sum(breadth_adv) / len(breadth_adv), 3) if breadth_adv else None
            ),
            "passed_days": breadth_passed_days,
            "blocked_days": breadth_blocked_days,
        },
        "no_trade_days": {
            "count": sum(no_trade_reasons.values()),
            "reasons": no_trade_reasons,
        },
        "round_trips": _summarize_round_trips(all_round_trips),
        # 차단 종목 반사실 — 차단된 종목들의 그날 평균 등락률(게이트 기회비용 신호).
        "blocked_outcome_avg_day_change_pct": (
            round(sum(blocked_day_changes) / len(blocked_day_changes), 2)
            if blocked_day_changes
            else None
        ),
    }


def _summarize_round_trips(round_trips: list[Any]) -> dict[str, Any]:
    """왕복 레코드 → 승률·평균 보유시간·청산사유별 성과. 표본 없으면 count=0."""
    priced = [rt for rt in round_trips if rt.pnl_pct is not None]
    holding = [rt.holding_minutes for rt in round_trips if rt.holding_minutes is not None]
    by_reason: dict[str, list[float]] = {}
    for rt in priced:
        by_reason.setdefault(rt.exit_reason or "unknown", []).append(rt.pnl_pct)
    wins = sum(1 for rt in priced if rt.pnl_pct > 0)
    return {
        "count": len(round_trips),
        "priced_samples": len(priced),
        "win_rate": round(wins / len(priced), 3) if priced else None,
        "avg_holding_minutes": round(sum(holding) / len(holding), 1) if holding else None,
        "by_exit_reason": {
            reason: {
                "n": len(pcts),
                "avg_pnl_pct": round(sum(pcts) / len(pcts), 2),
            }
            for reason, pcts in sorted(by_reason.items())
        },
    }


# ---- 호출 ----------------------------------------------------------------


def insight(
    reports: list[DailyReport],
    *,
    client: _AnthropicLike,
    model: str = "claude-opus-4-7",
    max_tokens: int = 1024,
) -> InsightResponse:
    """LLM 호출 → InsightResponse. 검증 실패 시 ValidationError raise.

    빈 reports 리스트는 호출자가 미리 거르는 게 표준 — 토큰 낭비 방지. 본 함수는
    빈 입력도 안전 처리하지만 LLM 응답이 의미 있을 가능성은 낮다.
    """
    payload = _summarize_reports(reports)
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
        tools=[PROPOSE_INSIGHT_TOOL],
        tool_choice={"type": "tool", "name": "propose_insight"},
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"다음 누적 데이터를 보고 propose_insight 도구를 호출해 주세요:\n{payload}"
                        ),
                    }
                ],
            }
        ],
    )
    record_response(tracker, response, model=model)
    tool_input = extract_tool_use_input(response, "propose_insight")
    return InsightResponse.model_validate(tool_input)


# ---- 포맷터 -------------------------------------------------------------


def format_insight_message(
    response: InsightResponse,
    *,
    days: int,
    date_range: tuple[str, str] | None,
) -> str:
    """텔레그램 / CLI 출력용 한국어 메시지.

    `report` 명령의 일일 리포트와 시각적으로 구분되도록 다른 이모지·헤더 사용.
    """
    range_label = f"{date_range[0]}~{date_range[1]}" if date_range else "데이터 부족"
    lines = [f"🧭 주간 인사이트 ({days}일 · {range_label})", "", response.summary_ko]
    if response.key_findings:
        lines.append("")
        lines.append("🔑 패턴")
        for f in response.key_findings:
            lines.append(f"• {f}")
    if response.recommendations:
        lines.append("")
        lines.append("🛠 권고")
        for r in response.recommendations:
            lines.append(f"• {r}")
    lines.append("")
    lines.append(f"신뢰도: {response.confidence:.2f}")
    return "\n".join(lines)


def build_insight_callback(
    client: _AnthropicLike,
    *,
    days: int = 7,
    reports_root: Path = Path("data/reports"),
    model: str = "claude-opus-4-7",
) -> Callable[[], Awaitable[str]]:
    """텔레그램 `/insight` → 직전 N일 리포트로 LLM 인사이트 생성하는 비동기 콜백.

    paper-day 러너 안에서 호출됨 — hot path 가 아니지만 LLM 콜은 동기 SDK.
    `asyncio.to_thread` 로 별도 스레드에서 돌려 이벤트 루프 안 막음.

    Anthropic 클라이언트 생성은 호출자 책임 — 키 없으면 호출자가 콜백을 주입하지
    않아야 한다(이 함수가 직접 Exit/검사하면 러너 시동을 막을 수 있음).
    """

    async def _runner() -> str:
        reports = collect_recent_reports(root=reports_root, days=days)
        if not reports:
            return (
                "🧭 인사이트 생성 불가 — 직전 일일 리포트 데이터가 없습니다.\n"
                "(paper-day 가 며칠 운영된 후 다시 시도하세요.)"
            )
        date_range = (reports[0].date, reports[-1].date)
        response = await asyncio.to_thread(insight, reports, client=client, model=model)
        return format_insight_message(response, days=len(reports), date_range=date_range)

    return _runner


__all__ = [
    "PROPOSE_INSIGHT_TOOL",
    "SYSTEM_PROMPT_KO",
    "InsightResponse",
    "build_insight_callback",
    "collect_recent_reports",
    "format_insight_message",
    "insight",
]
