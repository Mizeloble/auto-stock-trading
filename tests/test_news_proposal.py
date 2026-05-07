"""24단계 Phase B — DaytradeProposal LLM 호출 + 추출 단위."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from auto_trading._common import KST
from auto_trading.news.collector import NewsItem
from auto_trading.news.proposal import (
    DaytradeProposal,
    format_proposal_message,
    propose_daytrade_profile,
)
from auto_trading.reports.daily import DailyReport


def _report(date: str = "2026-05-06") -> DailyReport:
    return DailyReport(
        date=date,
        mode="day-trade",
        dry_run=True,
        discovery_chosen=["005930"],
        discovery_blocked=[],
        market_gates=[],
        buys=[],
        liquidations=[],
        error_count=0,
        note_ko="t",
        realized_pnl=1000.0,
        realized_pnl_pct=1.0,
    )


def _news() -> list[NewsItem]:
    return [
        NewsItem(
            title="삼성전자 신고가",
            summary="실적 호조",
            url="https://h.com/1",
            published_at=datetime(2026, 5, 6, 9, 0, tzinfo=KST),
            source="한국경제",
            tickers=("005930",),
        )
    ]


def _tool_use_response(input_dict: dict[str, Any]) -> Any:
    """anthropic SDK 응답을 흉내내는 가짜 — content[0] 는 tool_use 블록."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = "propose_daytrade_profile"
    block.input = input_dict
    response = MagicMock()
    response.content = [block]
    return response


def _client_with_response(input_dict: dict[str, Any]) -> Any:
    client = MagicMock()
    client.messages.create.return_value = _tool_use_response(input_dict)
    return client


def test_propose_minimal_response_validates() -> None:
    """필수 3필드만 있어도 통과 — 변경 안 할 옵션은 None."""
    client = _client_with_response(
        {
            "summary_ko": "유지",
            "rationale_ko": "지난 주 안정적 — 큰 변경 없음.",
            "confidence": 0.5,
        }
    )
    proposal = propose_daytrade_profile([_report()], _news(), client=client)
    assert proposal.summary_ko == "유지"
    assert proposal.top_n is None
    assert proposal.daily_loss_kill is None


def test_propose_with_changes_extracts_fields() -> None:
    """변경 필드까지 채워온 응답 — DaytradeProposal 의 모든 필드 검증."""
    client = _client_with_response(
        {
            "summary_ko": "방어적 전환",
            "rationale_ko": "변동성 ↑, 손절 강화.",
            "confidence": 0.7,
            "top_n": 2,
            "stop_loss_pct": 0.012,
            "trailing_stop_pct": 0.006,
            "daily_loss_kill": True,
            "themes_to_watch": ["반도체", "AI"],
            "symbols_to_avoid": ["xxx"],
        }
    )
    proposal = propose_daytrade_profile([_report()], _news(), client=client)
    assert proposal.top_n == 2
    assert proposal.stop_loss_pct == 0.012
    assert proposal.daily_loss_kill is True
    assert proposal.themes_to_watch == ["반도체", "AI"]


def test_propose_invalid_response_raises_validation_error() -> None:
    """tool_use input 이 schema 위반이면 ValidationError."""
    client = _client_with_response(
        {
            "summary_ko": "x",
            "rationale_ko": "x",
            "confidence": 99,  # 0~1 범위 위반
        }
    )
    with pytest.raises(ValidationError):
        propose_daytrade_profile([_report()], _news(), client=client)


def test_propose_no_tool_use_block_raises_value_error() -> None:
    """tool_use 블록 없는 응답 — ValueError."""
    response = MagicMock()
    response.content = []
    client = MagicMock()
    client.messages.create.return_value = response
    with pytest.raises(ValueError, match="tool_use"):
        propose_daytrade_profile([_report()], _news(), client=client)


def test_propose_passes_payload_to_llm() -> None:
    """messages.create 의 user message 에 reports + news + current_profile 모두 들어가는지."""
    client = _client_with_response({"summary_ko": "ok", "rationale_ko": "ok", "confidence": 0.5})
    propose_daytrade_profile(
        [_report()],
        _news(),
        client=client,
        current_profile={"top_n": 3, "risk": {"stop_loss_pct": 0.01}},
    )
    call = client.messages.create.call_args
    user_text = call.kwargs["messages"][0]["content"][0]["text"]
    assert "005930" in user_text
    assert "삼성전자" in user_text
    assert "stop_loss_pct" in user_text


def test_propose_uses_anthropic_caching_and_forced_tool() -> None:
    """system 프롬프트에 cache_control + tool_choice 가 propose_daytrade_profile 강제."""
    client = _client_with_response({"summary_ko": "ok", "rationale_ko": "ok", "confidence": 0.5})
    propose_daytrade_profile([_report()], _news(), client=client)
    kwargs = client.messages.create.call_args.kwargs
    assert kwargs["system"][0]["cache_control"]["type"] == "ephemeral"
    assert kwargs["tool_choice"] == {
        "type": "tool",
        "name": "propose_daytrade_profile",
    }


def test_format_proposal_message_includes_changes_and_themes() -> None:
    p = DaytradeProposal(
        summary_ko="방어",
        rationale_ko="r",
        confidence=0.7,
        top_n=2,
        stop_loss_pct=0.01,
        themes_to_watch=["반도체"],
        symbols_to_avoid=["xxx"],
    )
    msg = format_proposal_message(p)
    assert "🗞 다음 주" in msg
    assert "top_n → 2" in msg
    assert "stop_loss_pct → 0.01" in msg
    assert "📡 주목 테마: 반도체" in msg
    assert "⛔ 회피 종목: xxx" in msg


def test_format_proposal_no_changes_omits_section() -> None:
    """모든 옵션 None 이면 변경 파라미터 섹션 없음."""
    p = DaytradeProposal(summary_ko="유지", rationale_ko="ok", confidence=0.5)
    msg = format_proposal_message(p)
    assert "변경 파라미터" not in msg
    assert "📡 주목 테마" not in msg
