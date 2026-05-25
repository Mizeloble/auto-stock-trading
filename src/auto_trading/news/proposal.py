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
from ..reports.daily import DailyReport, merge_symbol_names, with_symbol_name
from ..strategy.scanner import RegimeReport
from .collector import NewsItem


class DaytradeProposal(BaseModel):
    """LLM 응답 검증. 모든 변경 필드는 옵션 — None 이면 "유지".

    근거 구조 (📝 섹션):
    - signals_ko: 봇이 입력 데이터에서 본 패턴들. 각 1문장, 정량 단서 포함.
    - judgment_ko: 시그널을 어떻게 해석했나 (2~3문장).
    - referenced_headlines_ko: 결정에 영향을 준 뉴스 헤드라인 인용.
    - weekly_memo_ko: 다음 주에 사용자가 봐줘야 할 1가지 (옵션).

    변경별 이유 (📌):
    - change_reasons[키]: 그 변경을 "왜 정확히 이 값으로" 바꿨는지 1~2문장. 키는
      변경 가능 필드명(top_n, stop_loss_pct, …)과 일치. 표시 단에서 매칭 안 되는
      키는 무시되니 LLM 이 키를 헷갈려도 안전.
    """

    summary_ko: str = Field(min_length=1, max_length=300)
    confidence: float = Field(ge=0.0, le=1.0)

    # 📝 근거 4섹션
    signals_ko: list[str] = Field(default_factory=list, max_length=5)
    judgment_ko: str = Field(default="", max_length=400)
    referenced_headlines_ko: list[str] = Field(default_factory=list, max_length=5)
    weekly_memo_ko: str | None = Field(default=None, max_length=200)

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

    # 📌 변경별 이유 — 키는 위 변경 필드명, 값은 1~2문장.
    change_reasons: dict[str, str] = Field(default_factory=dict, max_length=20)

    themes_to_watch: list[str] = Field(default_factory=list, max_length=10)
    symbols_to_avoid: list[str] = Field(default_factory=list, max_length=20)


class _AnthropicLike(Protocol):
    @property
    def messages(self) -> Any: ...


SYSTEM_PROMPT_KO = """\
당신은 한국 주식 day-trade 자동매매 봇의 주간 운영 분석가입니다.
지난 주의 (a) 일일 트레이딩 결과 (b) 시장 뉴스 헤드라인 (c) 현재 시장국면(regime) 을
보고, 다음 주에 적용할 day-trade 프로파일의 파라미터를 propose_daytrade_profile
도구로 제안하세요. regime 이 없으면(데이터 부족) reports + news 만 보고 판단합니다.

regime 별 일반 가이드:
- trend_up: 추세추종 유리. 손절(stop_loss_pct) 0.012~0.018 로 약간 풀고 진입 적극.
- trend_down: 하락 추세. 신규 진입 자제, momentum_threshold_pct 활성 또는 top_n 축소.
- range: 박스. 좁은 손절·익절 (stop_loss_pct ~0.008, partial_take_pct ~0.010).
- high_vol: 고변동성. 손절을 더 넓히거나 daily_loss_kill=True 로 안전 우선.

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

규칙 (필드 작성):
- 변경 가능 파라미터(top_n, cooldown_min, …)는 **바꿀 것만** 채우고 유지할 것은 비워두세요(필드 자체를 응답에 포함하지 말 것).
- summary_ko: 한국어 1~3문장 — 다음 주 핵심 변경의 한 줄 요약.
- signals_ko: 입력 데이터에서 본 **정량적 패턴 2~4개**. 각 1문장. 숫자(건수·%·일수) 를 가능한 한 인용. 예) "지난 7일 매수 18건 중 13건이 1.2~1.5% 구간에서 손절, 평균 보유 8분 → 노이즈 손절 우세".
- judgment_ko: 위 시그널을 어떻게 해석했나 (2~3문장). 결론과 근거를 함께. 예) "ATR(일간 평균 변동폭) 4.27% 환경에서 1.2% 손절은 일중 노이즈와 겹쳐 발동이 잦음. 1.5~1.8% 가 ATR 의 0.35~0.42배로 안전 마진 확보 가능".
- referenced_headlines_ko: 결정에 직접 영향을 준 뉴스 헤드라인을 짧게 인용 2~3개. 한 줄에 1개. 예) "원·달러 1,500원 고착 — 외환 변동성 장기화".
- weekly_memo_ko (옵션): 다음 주 사용자가 특별히 봐줘야 할 1가지. 예) "FOMC 결과에 따라 daily_loss_kill 발동 가능성 ↑, 모니터링 권장".
- change_reasons: 바꾼 파라미터마다 **그 변경의 정확한 이유 1~2문장**. 키는 변경 필드명(top_n, stop_loss_pct 등)과 일치. 예) {"stop_loss_pct": "손절 발동 13건 중 9건이 노이즈(재진입 후 +0.8% 반등). 1.8% 로 넓혀 ATR 안전 마진 확보."}.
- themes_to_watch: 다음 주 주목할 섹터/테마 (최대 10).
- symbols_to_avoid: 다음 주 피할 종목 코드 (최대 20). 이 필드는 코드만(예: "005930") — 종목명은 시스템이 표시 단계에서 붙임.
- confidence: 0.0~1.0, 데이터 충실도·일관성 기반.
- 어디서든 종목 언급 시 "코드 종목명" 형식(예: "005930 삼성전자"). 입력의 reports.symbol_names 와 chosen_freq_top.name 참고.
- 추가 텍스트 응답 없이 propose_daytrade_profile 도구 호출만 하세요.

작성 톤 (사용자는 주식 초보자):
- 영문 약자·지표명(ATR, BB폭, regime, momentum, ADX 등)은 처음 등장할 때 괄호로 한국어 풀이를 붙이세요. 예) "ATR(일간 평균 변동폭) 4.27%".
- YAML 키(stop_loss_pct, cooldown_min 등)를 본문에 그대로 쓰지 말고 사람말로. 예) "stop_loss_pct" → "손절선", "cooldown_min" → "재진입 대기 시간". 키가 꼭 필요하면 사람말 옆에 괄호로.
- 한 문장에 전문용어를 2개 이상 동시에 쓰지 마세요. 한 문장에 한 개념씩.
- 가능하면 일상 비유 1~2개 ("노이즈에 흔들리지 않게", "급등 초기에 못 잡더라도 …" 등) 를 섞어 직관을 돕습니다.
- change_reasons 본문에서 **수치를 인용** 하세요 — "그래서 왜 이 값인가" 가 숫자로 뒷받침되어야 사용자가 납득합니다.
"""

PROPOSE_TOOL = {
    "name": "propose_daytrade_profile",
    "description": "다음 주 day-trade 프로파일의 파라미터 변경 제안.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary_ko": {"type": "string", "minLength": 1, "maxLength": 300},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "signals_ko": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 200},
                "maxItems": 5,
                "description": "봇이 입력 데이터에서 본 패턴 (각 1문장, 정량 단서 포함).",
            },
            "judgment_ko": {
                "type": "string",
                "minLength": 1,
                "maxLength": 400,
                "description": "시그널을 어떻게 해석했나 (2~3문장).",
            },
            "referenced_headlines_ko": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 200},
                "maxItems": 5,
                "description": "결정에 영향을 준 뉴스 헤드라인 인용.",
            },
            "weekly_memo_ko": {
                "type": "string",
                "maxLength": 200,
                "description": "다음 주 사용자가 봐줘야 할 1가지 (옵션).",
            },
            "top_n": {"type": "integer", "minimum": 1, "maximum": 10},
            "cooldown_min": {"type": "integer", "minimum": 0, "maximum": 240},
            "momentum_lookback_min": {"type": "integer", "minimum": 1, "maximum": 60},
            "momentum_threshold_pct": {"type": "number", "minimum": 0.0, "maximum": 10.0},
            "stop_loss_pct": {"type": "number", "minimum": 0.0, "maximum": 0.10},
            "trailing_stop_pct": {"type": "number", "minimum": 0.0, "maximum": 0.10},
            "partial_take_pct": {"type": "number", "minimum": 0.0, "maximum": 0.10},
            "partial_take_qty_ratio": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "daily_loss_kill": {"type": "boolean"},
            "change_reasons": {
                "type": "object",
                "description": (
                    "변경 키 → 1~2문장 이유. 키는 변경 가능 필드명(top_n, stop_loss_pct, "
                    "cooldown_min 등)과 일치해야 표시됨."
                ),
                "additionalProperties": {"type": "string", "minLength": 1, "maxLength": 300},
            },
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
        "required": ["summary_ko", "signals_ko", "judgment_ko", "confidence"],
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
    symbol_names = merge_symbol_names(items)
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
        "symbol_names": symbol_names,
        "chosen_freq_top": [
            {"symbol": sym, "name": symbol_names.get(sym), "days": cnt}
            for sym, cnt in sorted(chosen_freq.items(), key=lambda kv: -kv[1])[:8]
        ],
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


def _summarize_regime(regime: RegimeReport | None) -> dict[str, Any]:
    """RegimeReport → LLM 입력용 dict. None 이면 빈 dict (LLM 이 알아서 처리)."""
    if regime is None:
        return {}
    return {
        "regime": regime.regime.value,
        "summary_ko": regime.summary_ko,
        "metrics": regime.metrics,
    }


def propose_daytrade_profile(
    reports: list[DailyReport],
    news: list[NewsItem],
    *,
    client: _AnthropicLike,
    current_profile: dict[str, Any] | None = None,
    regime: RegimeReport | None = None,
    model: str = "claude-opus-4-7",
    max_tokens: int = 4096,
    max_news_items: int = 80,
) -> DaytradeProposal:
    """LLM 호출 → DaytradeProposal. 검증 실패 시 ValidationError raise.

    `max_news_items` — 수집된 뉴스 중 LLM 입력으로 보낼 상한. default 80
    (호출당 input ~25k tok ≈ $0.45, 주 1회 cron 기준 월 ~$2).

    `regime` — KODEX 200 등 시장 대표 종목의 분류 결과. None 이면 LLM 이
    reports + news 만 보고 판단.
    """
    payload = {
        "reports": _summarize_reports(reports),
        "news": _summarize_news(news, max_items=max_news_items),
        "current_profile": _summarize_current_profile(current_profile),
        "regime": _summarize_regime(regime),
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


def format_proposal_message(
    proposal: DaytradeProposal,
    *,
    symbol_names: dict[str, str] | None = None,
) -> str:
    """텔레그램 / CLI 출력용 한국어 메시지 — Phase C 가 YAML 갱신 후 같이 푸시.

    `symbol_names` — 회피 종목 코드를 "코드 종목명" 으로 표시하기 위한 코드→이름 맵.
    호출자가 `merge_symbol_names(reports)` 로 만들어 전달. 이름 없으면 코드만(폴백).
    """
    names = symbol_names or {}
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
        lines.append(
            "⛔ 회피 종목: "
            + ", ".join(with_symbol_name(s, names.get(s)) for s in proposal.symbols_to_avoid)
        )
    lines.append("")
    lines.append("📝 근거")
    if proposal.signals_ko:
        lines.append("📍 시그널")
        for s in proposal.signals_ko:
            lines.append(f"  • {s}")
    if proposal.judgment_ko:
        lines.append("🎯 판단")
        lines.append(f"  {proposal.judgment_ko}")
    if proposal.referenced_headlines_ko:
        lines.append("🗞 영향 헤드라인")
        for h in proposal.referenced_headlines_ko:
            lines.append(f"  • {h}")
    if proposal.weekly_memo_ko:
        lines.append("📅 다음 주 메모")
        lines.append(f"  {proposal.weekly_memo_ko}")
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
