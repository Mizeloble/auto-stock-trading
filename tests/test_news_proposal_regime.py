"""propose_daytrade_profile + run_weekly_proposal_cycle 의 regime 통합 회귀.

29단계 PR18 — 시장국면을 LLM 입력에 추가.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from auto_trading.news.cli_helpers import WeeklyInputs, run_weekly_proposal_cycle
from auto_trading.news.proposal import _summarize_regime, propose_daytrade_profile
from auto_trading.strategy.scanner import MarketRegime, RegimeReport


def _mock_response(usage_input: int = 1000, usage_output: int = 200) -> SimpleNamespace:
    """anthropic.Message stub — tool_use 한 block 만."""
    tool_use = SimpleNamespace(
        type="tool_use",
        name="propose_daytrade_profile",
        input={
            "summary_ko": "다음 주는 보수적으로",
            "rationale_ko": "고변동성 + 일일 손실 패턴",
            "confidence": 0.7,
        },
    )
    return SimpleNamespace(
        content=[tool_use],
        usage=SimpleNamespace(input_tokens=usage_input, output_tokens=usage_output),
    )


def _regime(kind: MarketRegime = MarketRegime.HIGH_VOL) -> RegimeReport:
    return RegimeReport(
        regime=kind,
        metrics={
            "slope_pct": 0.005,
            "atr_pct": 0.031,
            "bb_width_pct": 0.38,
            "last_close": 121_800.0,
            "bars": 60.0,
        },
        summary_ko="고변동성 (ATR 3.10% / 봉) — 신호가 휩쓸릴 위험.",
    )


# ---- _summarize_regime ------------------------------------------------------


def test_summarize_regime_none_returns_empty_dict() -> None:
    assert _summarize_regime(None) == {}


def test_summarize_regime_includes_regime_summary_metrics() -> None:
    out = _summarize_regime(_regime())
    assert out["regime"] == "high_vol"
    assert "summary_ko" in out
    assert out["metrics"]["atr_pct"] == 0.031


# ---- propose_daytrade_profile regime payload --------------------------------


def test_propose_includes_regime_in_user_message() -> None:
    """regime 이 LLM user message 의 payload 에 들어가는지."""
    client = MagicMock()
    client.messages.create.return_value = _mock_response()

    propose_daytrade_profile(
        reports=[],
        news=[],
        client=client,
        regime=_regime(MarketRegime.TREND_UP),
    )

    call = client.messages.create.call_args
    user_text = call.kwargs["messages"][0]["content"][0]["text"]
    # payload 에 regime 정보가 직렬화돼서 들어갔는지.
    assert "trend_up" in user_text
    assert "regime" in user_text


def test_propose_without_regime_still_works() -> None:
    """regime 미주입 시 payload 의 regime 키는 빈 dict."""
    client = MagicMock()
    client.messages.create.return_value = _mock_response()

    propose_daytrade_profile(reports=[], news=[], client=client)  # regime 생략

    call = client.messages.create.call_args
    user_text = call.kwargs["messages"][0]["content"][0]["text"]
    assert "'regime': {}" in user_text or '"regime": {}' in user_text


# ---- run_weekly_proposal_cycle regime forwarding ----------------------------


def test_cycle_forwards_regime_to_propose(tmp_path: Path) -> None:
    """cli_helpers 가 regime 을 propose_daytrade_profile 로 그대로 전달."""
    current = tmp_path / "current.yaml"
    fallback = tmp_path / "default.yaml"
    # apply_and_persist 는 호출되어야 하므로 valid YAML 만들기.
    fallback.write_text(
        "name: x\nstrategy: index_momentum_daytrade\ntop_n: 3\nliquidate: '15:15'\n",
        encoding="utf-8",
    )

    client = MagicMock()
    client.messages.create.return_value = _mock_response()
    regime = _regime(MarketRegime.RANGE)

    run_weekly_proposal_cycle(
        WeeklyInputs(reports=[], news=[]),
        current_path=current,
        fallback_path=fallback,
        target_path=current,
        client=client,
        model="claude-opus-4-7",
        max_news_items=10,
        regime=regime,
    )

    call = client.messages.create.call_args
    user_text = call.kwargs["messages"][0]["content"][0]["text"]
    assert "range" in user_text


def test_cycle_with_no_regime_omits_regime_data(tmp_path: Path) -> None:
    """regime 인자 생략 시 None 으로 전달 → payload regime 은 빈 dict."""
    current = tmp_path / "current.yaml"
    fallback = tmp_path / "default.yaml"
    fallback.write_text(
        "name: x\nstrategy: index_momentum_daytrade\ntop_n: 3\nliquidate: '15:15'\n",
        encoding="utf-8",
    )

    client = MagicMock()
    client.messages.create.return_value = _mock_response()

    run_weekly_proposal_cycle(
        WeeklyInputs(reports=[], news=[]),
        current_path=current,
        fallback_path=fallback,
        target_path=current,
        client=client,
        model="claude-opus-4-7",
        max_news_items=10,
        # regime 생략 — default None.
    )

    call = client.messages.create.call_args
    user_text = call.kwargs["messages"][0]["content"][0]["text"]
    # 어떤 regime 카테고리 라벨도 들어가지 않음.
    for label in ("trend_up", "trend_down", "range", "high_vol"):
        assert label not in user_text
