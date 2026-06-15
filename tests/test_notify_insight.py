"""LLM 인사이트 — Anthropic stub 으로 tool_use 파싱·캐시·검증 회귀."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from auto_trading.notify.insight import (
    InsightResponse,
    _summarize_reports,
    collect_recent_reports,
    format_insight_message,
    insight,
)
from auto_trading.reports.daily import (
    DailyReport,
    LiquidationRecord,
    MarketGateInfo,
    TradeRecord,
    save_daily_report,
)


def _report(d: str, **overrides: Any) -> DailyReport:
    base: dict[str, Any] = dict(
        date=d,
        mode="day-trade",
        dry_run=True,
        discovery_chosen=["A"],
        discovery_blocked=[],
        market_gates=[MarketGateInfo(market="KOSPI", last=1.0, sma5=1.0, sma20=1.0, above=True)],
        buys=[TradeRecord(symbol="A", side="buy", quantity=1, price=100.0, simulated=True)],
        liquidations=[
            LiquidationRecord(
                symbol="A", quantity=1, sell_price=110.0, buy_price=100.0, simulated=True
            )
        ],
        error_count=0,
        note_ko="x",
        realized_pnl=10.0,
        realized_pnl_pct=10.0,
    )
    base.update(overrides)
    return DailyReport(**base)


# ---- collect_recent_reports ---------------------------------------------


def test_collect_recent_reports_orders_by_date_and_caps_to_n(tmp_path: Path) -> None:
    """파일명 정렬(YYYY-MM-DD) + 최근 N개 컷."""
    for d in ["2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05"]:
        save_daily_report(_report(d), root=tmp_path)
    reports = collect_recent_reports(root=tmp_path, days=3)
    assert [r.date for r in reports] == ["2026-05-03", "2026-05-04", "2026-05-05"]


def test_collect_returns_empty_on_missing_root(tmp_path: Path) -> None:
    """폴더 없거나 파일 없으면 빈 리스트 — 호출자가 데이터 부족 분기."""
    assert collect_recent_reports(root=tmp_path / "missing", days=7) == []


def test_collect_skips_corrupt_json_files(tmp_path: Path) -> None:
    save_daily_report(_report("2026-05-05"), root=tmp_path)
    (tmp_path / "daily-corrupt.json").write_text("{ broken json", encoding="utf-8")
    reports = collect_recent_reports(root=tmp_path, days=7)
    # 손상된 파일은 skip — 정상 파일만 1건.
    assert len(reports) == 1
    assert reports[0].date == "2026-05-05"


# ---- summarize -----------------------------------------------------------


def test_summarize_aggregates_chosen_freq_and_pnl_stats() -> None:
    reports = [
        _report(
            "2026-05-01",
            discovery_chosen=["A", "B"],
            buys=[TradeRecord(symbol="A", side="buy", quantity=1, price=100.0, simulated=True)],
            liquidations=[
                LiquidationRecord(
                    symbol="A",
                    quantity=1,
                    sell_price=110.0,
                    buy_price=100.0,
                    simulated=True,
                )
            ],
            realized_pnl=10.0,
            realized_pnl_pct=10.0,
        ),
        _report(
            "2026-05-02",
            discovery_chosen=["A"],
            buys=[],
            liquidations=[],
            realized_pnl=None,
            realized_pnl_pct=None,
        ),
    ]
    payload = _summarize_reports(reports)
    assert payload["days"] == 2
    assert payload["date_range"] == ["2026-05-01", "2026-05-02"]
    # A 가 2회, B 가 1회.
    chosen = {e["symbol"]: e["days"] for e in payload["chosen_freq_top"]}
    assert chosen["A"] == 2 and chosen["B"] == 1
    # 종목명 맵 키 존재 (이름 미지정 리포트면 빈 dict).
    assert "symbol_names" in payload
    assert payload["realized_pnl"]["samples"] == 1
    assert payload["realized_pnl"]["sum"] == 10.0


def test_summarize_aggregates_round_trips_regime_breadth_and_no_trade() -> None:
    """신규 데이터 기반 집계 — 승률·사유별 성과·국면·시장폭·미진입·반사실."""
    from auto_trading.reports.daily import BlockedOutcome, RoundTrip

    reports = [
        _report(
            "2026-05-01",
            regime="high_vol",
            breadth_advancing_pct=0.8,
            breadth_passed=True,
            round_trips=[
                RoundTrip(
                    symbol="A", name="에이", quantity=1, buy_price=100.0, sell_price=110.0,
                    buy_time_kst="09:05", sell_time_kst="09:35", holding_minutes=30,
                    pnl=10.0, pnl_pct=10.0, exit_reason="partial_take",
                ),
                RoundTrip(
                    symbol="B", name="비", quantity=1, buy_price=100.0, sell_price=95.0,
                    buy_time_kst="10:00", sell_time_kst="10:10", holding_minutes=10,
                    pnl=-5.0, pnl_pct=-5.0, exit_reason="stop_loss",
                ),
            ],
        ),
        _report(
            "2026-05-02",
            buys=[],
            liquidations=[],
            realized_pnl=None,
            realized_pnl_pct=None,
            regime="high_vol",
            breadth_passed=False,
            no_trade_reason="시장 폭 게이트 차단",
            discovery_blocked=[("C", "breadth_low")],
            blocked_outcomes=[
                BlockedOutcome(symbol="C", name="씨", reason="breadth_low",
                               intraday_pct=4.0, day_change_pct=5.0),
            ],
        ),
    ]
    payload = _summarize_reports(reports)
    assert payload["regime_days"] == {"high_vol": 2}
    assert payload["breadth"]["passed_days"] == 1
    assert payload["breadth"]["blocked_days"] == 1
    assert payload["no_trade_days"]["count"] == 1
    rt = payload["round_trips"]
    assert rt["count"] == 2
    assert rt["win_rate"] == 0.5  # 1승 1패
    assert rt["avg_holding_minutes"] == 20.0  # (30+10)/2
    assert rt["by_exit_reason"]["partial_take"]["avg_pnl_pct"] == 10.0
    assert rt["by_exit_reason"]["stop_loss"]["avg_pnl_pct"] == -5.0
    # 차단 종목이 그날 평균 +5% — 게이트 기회비용 신호.
    assert payload["blocked_outcome_avg_day_change_pct"] == 5.0


def test_summarize_zero_days_returns_minimal_payload() -> None:
    payload = _summarize_reports([])
    assert payload == {"days": 0}


# ---- insight (LLM 호출) --------------------------------------------------


def _stub_anthropic_with_tool_use(tool_input: dict) -> Any:
    """messages.create 가 tool_use 블록을 반환하는 SDK stub."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = "propose_insight"
    block.input = tool_input

    response = MagicMock()
    response.content = [block]

    client = MagicMock()
    client.messages.create.return_value = response
    return client


def test_insight_parses_tool_use_into_response_model() -> None:
    client = _stub_anthropic_with_tool_use(
        {
            "summary_ko": "이번 주 운영 안정.",
            "key_findings": ["A 가 4회 연속 발굴", "에러 0"],
            "recommendations": ["top_n 유지"],
            "confidence": 0.8,
        }
    )
    response = insight([_report("2026-05-05")], client=client)
    assert isinstance(response, InsightResponse)
    assert response.summary_ko == "이번 주 운영 안정."
    assert response.key_findings == ["A 가 4회 연속 발굴", "에러 0"]
    assert response.confidence == 0.8


def test_insight_uses_system_prompt_caching_and_propose_insight_tool() -> None:
    """system 프롬프트에 cache_control + propose_insight tool 강제 호출 확인."""
    client = _stub_anthropic_with_tool_use(
        {
            "summary_ko": "x",
            "key_findings": [],
            "recommendations": [],
            "confidence": 0.5,
        }
    )
    insight([_report("2026-05-05")], client=client, model="m")
    call_kwargs = client.messages.create.call_args.kwargs
    assert call_kwargs["model"] == "m"
    system = call_kwargs["system"]
    assert isinstance(system, list) and system[0]["cache_control"] == {"type": "ephemeral"}
    tools = call_kwargs["tools"]
    assert tools[0]["name"] == "propose_insight"
    assert call_kwargs["tool_choice"] == {"type": "tool", "name": "propose_insight"}


def test_insight_dict_block_compatibility() -> None:
    """SDK 가 block 을 dict 로 반환할 때도 파싱."""
    block_as_dict = {
        "type": "tool_use",
        "name": "propose_insight",
        "input": {
            "summary_ko": "ok",
            "key_findings": [],
            "recommendations": [],
            "confidence": 0.5,
        },
    }
    response = MagicMock()
    response.content = [block_as_dict]
    client = MagicMock()
    client.messages.create.return_value = response
    out = insight([_report("2026-05-05")], client=client)
    assert out.summary_ko == "ok"


def test_insight_rejects_response_without_tool_use() -> None:
    response = MagicMock()
    response.content = []
    client = MagicMock()
    client.messages.create.return_value = response
    with pytest.raises(ValueError, match="propose_insight"):
        insight([_report("2026-05-05")], client=client)


def test_insight_rejects_invalid_confidence() -> None:
    """confidence > 1.0 은 ValidationError."""
    from pydantic import ValidationError

    client = _stub_anthropic_with_tool_use(
        {
            "summary_ko": "x",
            "key_findings": [],
            "recommendations": [],
            "confidence": 1.5,
        }
    )
    with pytest.raises(ValidationError):
        insight([_report("2026-05-05")], client=client)


# ---- format_insight_message ---------------------------------------------


def test_format_insight_includes_findings_and_recommendations() -> None:
    response = InsightResponse(
        summary_ko="요약",
        key_findings=["발견 1", "발견 2"],
        recommendations=["권고 1"],
        confidence=0.75,
    )
    msg = format_insight_message(response, days=7, date_range=("2026-05-01", "2026-05-05"))
    assert "🧭 주간 인사이트 (7일 · 2026-05-01~2026-05-05)" in msg
    assert "요약" in msg
    assert "🔑 패턴" in msg
    assert "발견 1" in msg
    assert "🛠 권고" in msg
    assert "권고 1" in msg
    assert "신뢰도: 0.75" in msg


def test_format_insight_no_date_range_label() -> None:
    response = InsightResponse(summary_ko="x", key_findings=[], recommendations=[], confidence=0.5)
    msg = format_insight_message(response, days=0, date_range=None)
    assert "데이터 부족" in msg
