"""KRX 휴장일 진입 가드 회귀 — paper / paper-day / weekly-proposal.

검증:
- 휴장일 호출 → exit 0 + 안내 메시지 + 텔레그램 1회.
- `--force` → 가드 우회.
- 평일 호출 → 정상 진행 (PaperRunner 생성 / LLM 호출 흐름까지 도달).
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import structlog
from typer.testing import CliRunner

from auto_trading import cli as cli_module
from auto_trading.cli import app
from auto_trading.runner import PaperRunner


@pytest.fixture(autouse=True)
def _reset_structlog() -> Generator[None]:
    yield
    logging.getLogger().handlers.clear()
    structlog.reset_defaults()


@pytest.fixture
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KIWOOM_APP_KEY", "k")
    monkeypatch.setenv("KIWOOM_APP_SECRET", "s")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    return tmp_path


def _patch_today(monkeypatch: pytest.MonkeyPatch, fixed: date) -> None:
    monkeypatch.setattr(cli_module.krx_calendar, "today_kst", lambda: fixed)


def test_paper_day_skips_on_holiday(isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """2026-05-05 어린이날 → exit 0 + skip 메시지 + PaperRunner 미생성."""
    _patch_today(monkeypatch, date(2026, 5, 5))
    fake_notifier = AsyncMock()

    with (
        patch.object(cli_module, "build_notifier", return_value=fake_notifier),
        patch.object(PaperRunner, "__init__") as runner_init,
    ):
        result = CliRunner().invoke(app, ["paper-day", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "휴장일" in result.output
    assert "paper-day" in result.output
    runner_init.assert_not_called()  # 가드에서 막혀 러너 생성 안 됨
    # 텔레그램 1회 + aclose 1회
    assert fake_notifier.notify.await_count == 1
    event, _kw = fake_notifier.notify.await_args.args, fake_notifier.notify.await_args.kwargs
    assert event[0] == "runner.holiday_skip"
    assert _kw["command"] == "paper-day"
    assert _kw["date"] == "2026-05-05"


def test_paper_day_force_bypasses_holiday(
    isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--force` 면 휴장일에도 PaperRunner 생성까지 진행."""
    _patch_today(monkeypatch, date(2026, 5, 5))

    real_init = PaperRunner.__init__
    captured: dict[str, PaperRunner] = {}

    def _capture(self, **kwargs):  # type: ignore[no-untyped-def]
        real_init(self, **kwargs)
        captured["r"] = self

    async def _no_op(*_a, **_kw):  # type: ignore[no-untyped-def]
        return None

    with (
        patch.object(PaperRunner, "__init__", _capture),
        patch.object(cli_module, "paper_day_main", AsyncMock(side_effect=_no_op)),
        patch.object(cli_module, "build_command_handler", return_value=None),
    ):
        result = CliRunner().invoke(app, ["paper-day", "--dry-run", "--force"])

    assert result.exit_code == 0, result.output
    assert "휴장일" not in result.output
    assert "r" in captured  # 가드 우회 → 러너 생성됨


def test_paper_day_proceeds_on_trading_day(
    isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """평일이면 가드 통과 → 러너 생성 (회귀)."""
    _patch_today(monkeypatch, date(2026, 5, 8))  # 금

    real_init = PaperRunner.__init__
    captured: dict[str, PaperRunner] = {}

    def _capture(self, **kwargs):  # type: ignore[no-untyped-def]
        real_init(self, **kwargs)
        captured["r"] = self

    async def _no_op(*_a, **_kw):  # type: ignore[no-untyped-def]
        return None

    with (
        patch.object(PaperRunner, "__init__", _capture),
        patch.object(cli_module, "paper_day_main", AsyncMock(side_effect=_no_op)),
        patch.object(cli_module, "build_command_handler", return_value=None),
    ):
        result = CliRunner().invoke(app, ["paper-day", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "r" in captured


def test_paper_skips_on_holiday(isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """swing `paper` 도 같은 가드를 사용."""
    _patch_today(monkeypatch, date(2026, 5, 25))  # 부처님오신날
    fake_notifier = AsyncMock()

    with (
        patch.object(cli_module, "build_notifier", return_value=fake_notifier),
        patch.object(PaperRunner, "__init__") as runner_init,
    ):
        result = CliRunner().invoke(
            app, ["paper", "--strategy", "sma_crossover", "--symbols", "005930", "--dry-run"]
        )

    assert result.exit_code == 0, result.output
    assert "휴장일" in result.output
    runner_init.assert_not_called()
    assert fake_notifier.notify.await_args.kwargs["command"] == "paper"


def test_weekly_proposal_skips_on_long_holiday_block(
    isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """다음 거래일까지 5일 이상 떨어진 토 → LLM skip.

    예: 2025-09-27 (토) — 다음 거래일은 추석 연휴 후 2025-10-02 (목 추석 마무리 후 첫 거래일).
    실제로 KRX_HOLIDAYS[2025] 기준 9-27(토) → 9-28(일) → 9-29(월/대체) → 10-1(수) → 10-2(목)
    까지 평일 갈 길이 멀어 gap >= 5.
    """
    # 2025-09-27 (토). 다음 거래일은 9-29(월) 평일이지만 추석은 10-6/7/8 이라
    # 실제 gap = 2 (9-29 월요일 거래일). 즉 일반 토요일 → skip 안 됨.
    # 진짜 긴 연휴 블록이 필요. 2026 추석: 9-24/25 휴장 + 토일 26/27 → 다음 거래일 9-28(월).
    # 9-19 (토) 부터 보면 9-21 월~9-23 수 평일 → skip 안 됨.
    # 실제로 5일 이상 gap 인 토요일 사례: 2025-12-27 (토) → 12-29 월 평일 (gap=2).
    # 2025 연말 12-31 휴장 + 2026-1-1 휴장 → 12-27(토) → 12-29(월) 거래일 gap=2.
    # 2026-09-19 (토) → 9-21(월) 거래 → gap=2.
    # 5일 이상 gap 만들기 어려움. 2026 설날: 2-14(토) → 2-16,17,18 휴장 + 2-19,20 평일 거래.
    #   → 2-14(토) 기준 다음 거래일 2-19(목). gap = 5. ✓
    _patch_today(monkeypatch, date(2026, 2, 14))  # 토, 다음 거래일 2-19(목), gap=5
    fake_notifier = AsyncMock()

    with (
        patch.object(cli_module, "build_notifier", return_value=fake_notifier),
        patch(
            "auto_trading.notify.insight.collect_recent_reports",
            return_value=[],
        ),
        patch("auto_trading.news.collector.collect_recent_news", return_value=[]),
        patch.object(cli_module, "_build_anthropic_client") as build_client,
    ):
        result = CliRunner().invoke(app, ["weekly-proposal"])

    assert result.exit_code == 0, result.output
    assert "weekly-proposal 시동 생략" in result.output
    build_client.assert_not_called()  # LLM 호출 0 — 비용 절약 확인


def test_weekly_proposal_runs_on_normal_saturday(
    isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """일반 토요일(다음 월요일이 평일) → 가드 통과해 데이터 부족 분기 도달."""
    _patch_today(monkeypatch, date(2026, 5, 9))  # 토, 다음 거래일 5-11(월), gap=2
    with patch("auto_trading.news.collector.collect_recent_news", return_value=[]):
        result = CliRunner().invoke(app, ["weekly-proposal"])
    # 가드 통과 → 데이터 부족 안내로 Exit(1)
    assert result.exit_code != 0
    assert "데이터 부족" in result.output


def test_weekly_proposal_force_overrides_holiday_block(
    isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--force` 는 긴 연휴여도 진행."""
    _patch_today(monkeypatch, date(2026, 2, 14))  # 토, gap=5
    with patch("auto_trading.news.collector.collect_recent_news", return_value=[]):
        result = CliRunner().invoke(app, ["weekly-proposal", "--force"])
    # 가드 우회 → 본문 진입 → 데이터 부족으로 Exit(1) (가드 skip 메시지 X)
    assert result.exit_code != 0
    assert "weekly-proposal 시동 생략" not in result.output
    assert "데이터 부족" in result.output
