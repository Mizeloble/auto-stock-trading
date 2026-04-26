"""LLM advisor 단위테스트 (Anthropic SDK stub, 네트워크 없음)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
import pytest
from pydantic import ValidationError

from auto_trading.strategy import llm_advisor as advisor
from auto_trading.strategy.profile import StrategyProfile
from auto_trading.strategy.scanner import MarketRegime, RegimeReport


class _FakeBlock:
    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


class _FakeResponse:
    def __init__(self, blocks: list[Any]) -> None:
        self.content = blocks


class _FakeMessages:
    def __init__(self, response: _FakeResponse) -> None:
        self.last_kwargs: dict[str, Any] | None = None
        self._response = response

    def create(self, **kwargs: Any) -> _FakeResponse:
        self.last_kwargs = kwargs
        return self._response


class _FakeAnthropic:
    def __init__(self, response: _FakeResponse) -> None:
        self.messages = _FakeMessages(response)


def _profile() -> StrategyProfile:
    return StrategyProfile(
        name="auto_005930",
        symbol="005930",
        strategy="rsi_reversion",
        params={"period": 14, "oversold": 30, "overbought": 70},
        weight=1.0,
        notes="박스권",
        created_at=datetime(2026, 4, 26, tzinfo=UTC),
    )


def _df(n: int = 30) -> pd.DataFrame:
    rows = [
        {
            "ts": datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=i),
            "open": 100.0 + i,
            "high": 101.0 + i,
            "low": 99.0 + i,
            "close": 100.0 + i,
            "volume": 1,
        }
        for i in range(n)
    ]
    return pd.DataFrame(rows).set_index("ts")


def _report() -> RegimeReport:
    return RegimeReport(
        regime=MarketRegime.TREND_UP,
        metrics={"slope_pct": 0.005, "atr_pct": 0.01, "bb_width_pct": 0.05},
        summary_ko="상승 추세",
    )


def test_advise_parses_tool_use_response() -> None:
    block = _FakeBlock(
        type="tool_use",
        name="propose_change",
        input={
            "strategy": "sma_crossover",
            "params": {"short": 5, "long": 20},
            "weight": 0.8,
            "reason_ko": "상승 추세 확인 — 추세추종 전환",
            "confidence": 0.75,
        },
    )
    client = _FakeAnthropic(_FakeResponse([block]))
    response = advisor.advise(_profile(), _df(), _report(), client=client)  # type: ignore[arg-type]

    assert response.change.strategy == "sma_crossover"
    assert response.change.params == {"short": 5, "long": 20}
    assert response.change.weight == 0.8
    assert "추세추종" in response.reason_ko
    assert response.confidence == 0.75


def test_advise_passes_system_with_cache_control() -> None:
    block = _FakeBlock(
        type="tool_use",
        name="propose_change",
        input={
            "strategy": "rsi_reversion",
            "params": {"period": 14, "oversold": 30, "overbought": 70},
            "weight": 1.0,
            "reason_ko": "변경 없음",
            "confidence": 0.5,
        },
    )
    client = _FakeAnthropic(_FakeResponse([block]))
    advisor.advise(_profile(), _df(), _report(), client=client, model="claude-haiku-4-5-20251001")  # type: ignore[arg-type]

    kwargs = client.messages.last_kwargs
    assert kwargs is not None
    assert kwargs["model"] == "claude-haiku-4-5-20251001"
    system = kwargs["system"]
    assert isinstance(system, list) and len(system) == 1
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert kwargs["tool_choice"] == {"type": "tool", "name": "propose_change"}


def test_advise_dict_block_is_also_supported() -> None:
    block = {
        "type": "tool_use",
        "name": "propose_change",
        "input": {
            "strategy": "rsi_reversion",
            "params": {"period": 7, "oversold": 25, "overbought": 75},
            "weight": 0.5,
            "reason_ko": "변동성 확대",
            "confidence": 0.6,
        },
    }
    client = _FakeAnthropic(_FakeResponse([block]))
    response = advisor.advise(_profile(), _df(), _report(), client=client)  # type: ignore[arg-type]
    assert response.change.params["period"] == 7


def test_advise_raises_when_no_tool_use_block() -> None:
    block = _FakeBlock(type="text", text="죄송하지만…")
    client = _FakeAnthropic(_FakeResponse([block]))
    with pytest.raises(ValueError, match="propose_change"):
        advisor.advise(_profile(), _df(), _report(), client=client)  # type: ignore[arg-type]


def test_advise_validates_weight_range() -> None:
    block = _FakeBlock(
        type="tool_use",
        name="propose_change",
        input={
            "strategy": "rsi_reversion",
            "params": {"period": 14, "oversold": 30, "overbought": 70},
            "weight": 1.5,  # 범위 밖
            "reason_ko": "왜인지 1.5",
            "confidence": 0.5,
        },
    )
    client = _FakeAnthropic(_FakeResponse([block]))
    with pytest.raises(ValidationError):
        advisor.advise(_profile(), _df(), _report(), client=client)  # type: ignore[arg-type]
