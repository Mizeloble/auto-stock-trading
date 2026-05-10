"""Claude API 기반 전략 방향성 조정.

`auto-trade strategy advise` 가 사람 명령으로만 트리거된다 — 자동 호출 없음.
입력: 현재 프로파일 + 시장국면 리포트 + 최근 종가 요약.
출력: pydantic `AdvisorResponse` (변경된 strategy/params/weight + 한국어 근거 + confidence).

비용 보호: system 프롬프트 캐싱(`cache_control: ephemeral`), OHLC 풀시리즈 대신 요약만 전송.
응답 구조 강제: tool_use(`propose_change`) 만 허용 → SDK dict 를 pydantic 으로 검증.
"""

from __future__ import annotations

from typing import Any, Protocol

import pandas as pd
from pydantic import BaseModel, Field

from ..llm import extract_tool_use_input
from .profile import StrategyName, StrategyProfile
from .scanner import RegimeReport


class AdvisorChange(BaseModel):
    strategy: StrategyName
    params: dict[str, int | float] = Field(default_factory=dict)
    weight: float = Field(ge=0.0, le=1.0)


class AdvisorResponse(BaseModel):
    change: AdvisorChange
    reason_ko: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)


class _AnthropicLike(Protocol):
    """anthropic.Anthropic 의 messages.create 호출만 사용 — DI 로 stub 가능."""

    @property
    def messages(self) -> Any: ...


SYSTEM_PROMPT_KO = """\
당신은 한국 주식 자동매매 봇의 전략 조정 어시스턴트입니다.
주어진 현재 프로파일·시장국면·최근 종가 요약을 보고, 변경이 필요하면 propose_change 도구를 호출해 새 전략·파라미터·비중을 제안하세요.

규칙:
- strategy 는 반드시 'sma_crossover' 또는 'rsi_reversion' 중 하나.
- sma_crossover params: {short: int>0, long: int>0, short<long}. 보통 short 3~10, long 15~30.
- rsi_reversion params: {period: int>=2, oversold: 0<x<overbought, overbought: oversold<x<100}. period 보통 7~21.
- weight 는 0.0~1.0. 추세 강세에는 1.0, 약세·고변동성에는 0.3~0.5.
- 변경 이유(reason_ko)는 한국어 한두 문장. 어떤 시장 신호에 근거했는지 구체적으로.
- confidence 는 0.0~1.0 으로 본인 확신도.
- 추가 텍스트 응답 없이 propose_change 도구 호출 결과만 내세요.
"""

PROPOSE_CHANGE_TOOL = {
    "name": "propose_change",
    "description": "프로파일에 적용할 새 전략 설정을 한 번만 제안한다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "strategy": {
                "type": "string",
                "enum": ["sma_crossover", "rsi_reversion"],
            },
            "params": {
                "type": "object",
                "additionalProperties": {"type": "number"},
            },
            "weight": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "reason_ko": {"type": "string", "minLength": 1},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        },
        "required": ["strategy", "params", "weight", "reason_ko", "confidence"],
    },
}


def _summarize_ohlc(df: pd.DataFrame, *, last_n: int = 30) -> dict[str, float | int]:
    """풀 OHLC 대신 마지막 N봉의 요약 통계만 — 토큰 절약."""
    tail = df.tail(last_n)
    closes = tail["close"].astype(float)
    return {
        "n": len(tail),
        "first_close": float(closes.iloc[0]) if not closes.empty else 0.0,
        "last_close": float(closes.iloc[-1]) if not closes.empty else 0.0,
        "min_close": float(closes.min()) if not closes.empty else 0.0,
        "max_close": float(closes.max()) if not closes.empty else 0.0,
        "mean_close": float(closes.mean()) if not closes.empty else 0.0,
        "std_close": float(closes.std() or 0.0) if not closes.empty else 0.0,
    }


def _build_user_payload(
    profile: StrategyProfile,
    df: pd.DataFrame,
    report: RegimeReport,
) -> dict[str, Any]:
    return {
        "profile": {
            "symbol": profile.symbol,
            "strategy": profile.strategy,
            "params": profile.params,
            "weight": profile.weight,
            "notes": profile.notes,
        },
        "regime": {
            "regime": report.regime.value,
            "metrics": report.metrics,
            "summary_ko": report.summary_ko,
        },
        "ohlc_summary": _summarize_ohlc(df),
    }


def advise(
    profile: StrategyProfile,
    df: pd.DataFrame,
    report: RegimeReport,
    *,
    client: _AnthropicLike,
    model: str = "claude-opus-4-7",
    max_tokens: int = 1024,
) -> AdvisorResponse:
    """LLM 호출 → AdvisorResponse. 검증 실패 시 ValidationError raise."""
    user_payload = _build_user_payload(profile, df, report)
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
        tools=[PROPOSE_CHANGE_TOOL],
        tool_choice={"type": "tool", "name": "propose_change"},
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"다음 입력을 보고 propose_change 도구를 호출해 주세요:\n{user_payload}"
                        ),
                    }
                ],
            }
        ],
    )

    tool_input = extract_tool_use_input(response, "propose_change")
    return AdvisorResponse.model_validate(
        {
            "change": {
                "strategy": tool_input["strategy"],
                "params": tool_input["params"],
                "weight": tool_input["weight"],
            },
            "reason_ko": tool_input["reason_ko"],
            "confidence": tool_input["confidence"],
        }
    )


__all__ = [
    "PROPOSE_CHANGE_TOOL",
    "SYSTEM_PROMPT_KO",
    "AdvisorChange",
    "AdvisorResponse",
    "advise",
]
