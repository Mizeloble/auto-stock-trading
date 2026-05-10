"""news/cli_helpers.py — should_skip + WeeklyInputs + run_weekly_proposal_cycle 단위 테스트.

cli.py 의 weekly_proposal 통합 테스트(test_cli_weekly_proposal.py) 와 별도로,
헬퍼 단위에서 휴장 가드 분기·LLM cycle wiring 을 격리 검증.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from auto_trading.news.cli_helpers import (
    WeeklyInputs,
    run_weekly_proposal_cycle,
    should_skip_weekly_proposal_for_holiday,
)

# -- should_skip_weekly_proposal_for_holiday --


def test_force_always_returns_none() -> None:
    """force=True 면 휴장 여부 무관 — 사용자가 수동 디버그/즉시 호출 의도."""
    with patch("auto_trading.news.cli_helpers.krx_calendar.is_trading_day", return_value=False):
        assert should_skip_weekly_proposal_for_holiday(force=True, today=date(2026, 5, 9)) is None


def test_skip_when_holiday_and_long_gap() -> None:
    """오늘이 휴장 + 다음 거래일까지 5일 이상 → 스킵 메시지 반환."""
    with (
        patch("auto_trading.news.cli_helpers.krx_calendar.is_trading_day", return_value=False),
        patch(
            "auto_trading.news.cli_helpers.krx_calendar.next_trading_day",
            return_value=date(2026, 5, 18),
        ),
    ):
        msg = should_skip_weekly_proposal_for_holiday(force=False, today=date(2026, 5, 9))
    assert msg is not None
    assert "9일" in msg  # gap_days = 18 - 9 = 9
    assert "weekly-proposal" in msg


def test_no_skip_on_normal_saturday() -> None:
    """평소 토요일(다음 평일까지 2일) — 휴장이지만 gap < 5 → 정상 진행."""
    with (
        patch("auto_trading.news.cli_helpers.krx_calendar.is_trading_day", return_value=False),
        patch(
            "auto_trading.news.cli_helpers.krx_calendar.next_trading_day",
            return_value=date(2026, 5, 11),
        ),
    ):
        assert should_skip_weekly_proposal_for_holiday(force=False, today=date(2026, 5, 9)) is None


def test_no_skip_on_trading_day() -> None:
    """오늘이 평일이면 무조건 None (gap 무관)."""
    with patch("auto_trading.news.cli_helpers.krx_calendar.is_trading_day", return_value=True):
        assert should_skip_weekly_proposal_for_holiday(force=False, today=date(2026, 5, 11)) is None


# -- WeeklyInputs --


def test_weekly_inputs_is_empty_only_when_both_empty() -> None:
    assert WeeklyInputs(reports=[], news=[]).is_empty is True
    assert WeeklyInputs(reports=[MagicMock()], news=[]).is_empty is False
    assert WeeklyInputs(reports=[], news=[MagicMock()]).is_empty is False


# -- run_weekly_proposal_cycle --


def test_run_cycle_target_path_defaults_to_current_when_none(tmp_path: Path) -> None:
    """target_path=None → current_path 그대로 덮어쓰기 (apply_and_persist 인자 검증)."""
    inputs = WeeklyInputs(reports=[], news=[])
    fake_proposal = MagicMock()
    fake_result = MagicMock()
    current = tmp_path / "current.yaml"
    fallback = tmp_path / "fallback.yaml"

    with (
        patch(
            "auto_trading.news.cli_helpers.propose_daytrade_profile",
            return_value=fake_proposal,
        ) as mock_propose,
        patch(
            "auto_trading.news.cli_helpers.apply_and_persist", return_value=fake_result
        ) as mock_apply,
        patch(
            "auto_trading.news.cli_helpers.format_apply_message",
            return_value="msg-ko",
        ) as mock_fmt,
    ):
        proposal, result, msg = run_weekly_proposal_cycle(
            inputs,
            current_path=current,
            fallback_path=fallback,
            target_path=None,
            client=MagicMock(),
            model="claude-opus-4-7",
            max_news_items=80,
        )

    assert proposal is fake_proposal
    assert result is fake_result
    assert msg == "msg-ko"
    mock_propose.assert_called_once()
    # target_path=None → current 로 fall back 했는지
    assert mock_apply.call_args.kwargs["target_path"] == current
    assert mock_apply.call_args.kwargs["current_path"] == current
    assert mock_apply.call_args.kwargs["fallback_path"] == fallback
    mock_fmt.assert_called_once_with(fake_result, fake_proposal)


def test_run_cycle_explicit_target_path_used(tmp_path: Path) -> None:
    """target_path 가 명시되면 current 와 다른 경로로 저장."""
    target = tmp_path / "target.yaml"
    current = tmp_path / "current.yaml"
    fallback = tmp_path / "fallback.yaml"

    with (
        patch("auto_trading.news.cli_helpers.propose_daytrade_profile", return_value=MagicMock()),
        patch(
            "auto_trading.news.cli_helpers.apply_and_persist", return_value=MagicMock()
        ) as mock_apply,
        patch("auto_trading.news.cli_helpers.format_apply_message", return_value="x"),
    ):
        run_weekly_proposal_cycle(
            WeeklyInputs(reports=[], news=[]),
            current_path=current,
            fallback_path=fallback,
            target_path=target,
            client=MagicMock(),
            model="m",
            max_news_items=10,
        )

    assert mock_apply.call_args.kwargs["target_path"] == target


def test_run_cycle_passes_max_news_items_to_propose(tmp_path: Path) -> None:
    """max_news_items 가 LLM 호출 인자로 그대로 전달 (토큰 캡 회귀 방지)."""
    with (
        patch(
            "auto_trading.news.cli_helpers.propose_daytrade_profile", return_value=MagicMock()
        ) as mock_propose,
        patch("auto_trading.news.cli_helpers.apply_and_persist", return_value=MagicMock()),
        patch("auto_trading.news.cli_helpers.format_apply_message", return_value="x"),
    ):
        run_weekly_proposal_cycle(
            WeeklyInputs(reports=[], news=[]),
            current_path=tmp_path / "c.yaml",
            fallback_path=tmp_path / "f.yaml",
            target_path=None,
            client=MagicMock(),
            model="claude-opus-4-7",
            max_news_items=50,
        )

    assert mock_propose.call_args.kwargs["max_news_items"] == 50
