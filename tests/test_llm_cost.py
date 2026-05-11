"""llm.cost 헬퍼 회귀.

29단계 PR15 (#16 2차).

검증:
- get_default_tracker — settings 의 llm_cost_path 가 None 이면 None.
- get_default_tracker — 활성 시 CostTracker (path + budget 반영).
- check_budget_or_raise — None tracker → no-op.
- check_budget_or_raise — under budget → no-op.
- check_budget_or_raise — over budget → LLMBudgetExceeded raise.
- record_response — None tracker → None.
- record_response — usage 있는 mock 응답 → record + CostEntry 반환.
- record_response — usage 없는 응답(mock SDK) → 조용히 None.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_trading._common import KST
from auto_trading.config import KiwoomMode, Settings
from auto_trading.llm.cost import (
    LLMBudgetExceeded,
    check_budget_or_raise,
    get_default_tracker,
    record_response,
)
from auto_trading.llm.cost_tracker import CostTracker


def _settings(path: Path | None = None, budget: float = 5.0) -> Settings:
    return Settings(
        kiwoom_mode=KiwoomMode.MOCK,
        kiwoom_app_key="k",
        kiwoom_app_secret="s",
        llm_cost_path=path,
        llm_monthly_budget_usd=budget,
    )


# ---- get_default_tracker -----------------------------------------------------


def test_get_default_tracker_returns_none_when_path_unset() -> None:
    assert get_default_tracker(_settings(path=None)) is None


def test_get_default_tracker_returns_tracker_when_path_set(tmp_path: Path) -> None:
    path = tmp_path / "cost.jsonl"
    tracker = get_default_tracker(_settings(path=path, budget=3.0))
    assert tracker is not None
    assert tracker.path == path
    assert tracker.monthly_budget_usd == 3.0


# ---- check_budget_or_raise --------------------------------------------------


def test_check_budget_or_raise_noop_for_none_tracker() -> None:
    check_budget_or_raise(None)  # 예외 X


def test_check_budget_or_raise_noop_when_under_budget(tmp_path: Path) -> None:
    tracker = CostTracker(path=tmp_path / "cost.jsonl", monthly_budget_usd=5.0)
    # 비어있는 트래커 — 0 사용량.
    check_budget_or_raise(tracker)


def test_check_budget_or_raise_raises_when_over_budget(tmp_path: Path) -> None:
    tracker = CostTracker(path=tmp_path / "cost.jsonl", monthly_budget_usd=0.5)
    ts = datetime.now(tz=KST)
    # $0.9 사용 → 180% over.
    tracker.record(model="claude-opus-4-7", input_tokens=10_000, output_tokens=10_000, ts=ts)

    with pytest.raises(LLMBudgetExceeded) as ei:
        check_budget_or_raise(tracker)
    assert ei.value.status.over is True


# ---- record_response --------------------------------------------------------


def _mock_response_with_usage(input_tokens: int, output_tokens: int) -> SimpleNamespace:
    return SimpleNamespace(
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
    )


def test_record_response_noop_for_none_tracker() -> None:
    response = _mock_response_with_usage(100, 50)
    assert record_response(None, response, model="claude-opus-4-7") is None


def test_record_response_appends_entry_for_active_tracker(tmp_path: Path) -> None:
    tracker = CostTracker(path=tmp_path / "cost.jsonl")
    response = _mock_response_with_usage(1_000, 500)

    entry = record_response(tracker, response, model="claude-opus-4-7")

    assert entry is not None
    assert entry.input_tokens == 1_000
    assert entry.output_tokens == 500
    assert entry.cost_usd > 0
    # JSONL 에 한 줄 append.
    lines = (tmp_path / "cost.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


def test_record_response_skips_when_response_has_no_usage(tmp_path: Path) -> None:
    """SDK 변경/mock 응답 등 usage 없으면 조용히 skip — 호출자 동작 X."""
    tracker = CostTracker(path=tmp_path / "cost.jsonl")
    response = SimpleNamespace()  # usage attribute 없음.

    entry = record_response(tracker, response, model="claude-opus-4-7")
    assert entry is None
    # 파일도 안 만들어짐.
    assert not (tmp_path / "cost.jsonl").exists() or (tmp_path / "cost.jsonl").read_text() == ""


def test_record_response_skips_zero_usage(tmp_path: Path) -> None:
    """0/0 토큰은 의미 없는 호출 — 기록 skip."""
    tracker = CostTracker(path=tmp_path / "cost.jsonl")
    response = _mock_response_with_usage(0, 0)
    assert record_response(tracker, response, model="claude-opus-4-7") is None


def test_llm_budget_exceeded_message_includes_status() -> None:
    """예외 메시지에 사용량 정보 포함 — 호출자가 사용자 안내에 그대로 사용."""
    from auto_trading.llm.cost_tracker import BudgetStatus

    status = BudgetStatus(
        year_month="2026-05",
        spent_usd=6.50,
        budget_usd=5.0,
        pct_used=1.30,
        alert=True,
        over=True,
    )
    exc = LLMBudgetExceeded(status)
    msg = str(exc)
    assert "$6.50" in msg
    assert "$5.00" in msg
    assert "2026-05" in msg
    assert exc.status is status


# ---- Settings 기반 시나리오 -------------------------------------------------


def test_get_default_tracker_uses_settings_budget(tmp_path: Path) -> None:
    """Settings 의 llm_monthly_budget_usd 가 default $5 외 다른 값이어도 적용."""
    custom_budget_path = tmp_path / "cost.jsonl"
    tracker = get_default_tracker(_settings(path=custom_budget_path, budget=10.0))
    assert tracker is not None
    assert tracker.monthly_budget_usd == 10.0


# 운영 시나리오 — 한도 초과 후 새 호출 차단되는지 (시뮬).


def test_full_scenario_record_then_block(tmp_path: Path) -> None:
    """기록 → 한도 초과 → 다음 호출에서 차단 (LLMBudgetExceeded)."""
    tracker = CostTracker(path=tmp_path / "cost.jsonl", monthly_budget_usd=0.5)
    ts = datetime.now(tz=KST) - timedelta(minutes=1)
    record_response(
        tracker,
        _mock_response_with_usage(10_000, 10_000),
        model="claude-opus-4-7",
    )
    # 직전 record 가 ts 자동 = now → budget 검사 시 같은 달 합산.

    with pytest.raises(LLMBudgetExceeded):
        check_budget_or_raise(tracker)
    _ = ts  # unused — 명시적 ts 필요 시점 회피
