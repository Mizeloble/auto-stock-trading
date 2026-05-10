"""`auto-trade weekly-proposal` 명령 회귀 — 24단계 Phase D."""

from __future__ import annotations

import logging
from collections.abc import Generator
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import structlog
from typer.testing import CliRunner

from auto_trading._common import KST
from auto_trading.cli import app
from auto_trading.daytrade.profile import DaytradeProfile
from auto_trading.news.collector import NewsItem
from auto_trading.reports.daily import DailyReport, save_daily_report


@pytest.fixture(autouse=True)
def _reset_structlog() -> Generator[None]:
    yield
    logging.getLogger().handlers.clear()
    structlog.reset_defaults()


def _seed_reports(root: Path, dates: list[str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for d in dates:
        save_daily_report(
            DailyReport(
                date=d,
                mode="day-trade",
                dry_run=True,
                discovery_chosen=["A"],
                discovery_blocked=[],
                market_gates=[],
                buys=[],
                liquidations=[],
                error_count=0,
                note_ko=f"{d} 요약",
                realized_pnl=100.0,
                realized_pnl_pct=0.5,
            ),
            root=root,
        )


def _seed_default_profile(root: Path) -> Path:
    target = root / "strategies" / "profiles" / "daytrade-default.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    DaytradeProfile(name="d").dump_yaml(target)
    return target


def _patch_news(items: list[NewsItem] | None = None):  # type: ignore[no-untyped-def]
    """RSS 실 호출을 막는 patch — 기본 빈 리스트."""
    return patch("auto_trading.news.collector.collect_recent_news", return_value=items or [])


def test_weekly_proposal_no_data_exits_with_hint(isolated_cwd: Path) -> None:
    """reports + news 모두 없으면 Exit(1) + 안내."""
    runner = CliRunner()
    with _patch_news():
        result = runner.invoke(app, ["weekly-proposal"])
    assert result.exit_code != 0
    assert "데이터 부족" in result.output


def test_weekly_proposal_dry_run_does_not_call_llm(
    isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--dry-run 은 LLM 호출 없이 입력 요약만 출력 + 키 없어도 동작."""
    _seed_reports(isolated_cwd / "data" / "reports", ["2026-05-05", "2026-05-06"])
    runner = CliRunner()
    with _patch_news():
        result = runner.invoke(app, ["weekly-proposal", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "reports" in result.output


def test_weekly_proposal_full_cycle(isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM mocking → propose → apply → daytrade-current.yaml 작성."""
    _seed_reports(isolated_cwd / "data" / "reports", ["2026-05-05", "2026-05-06"])
    fallback = _seed_default_profile(isolated_cwd)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")

    block = MagicMock()
    block.type = "tool_use"
    block.name = "propose_daytrade_profile"
    block.input = {
        "summary_ko": "test",
        "rationale_ko": "test rationale",
        "confidence": 0.5,
        "top_n": 2,
    }
    response = MagicMock()
    response.content = [block]
    fake_client = MagicMock()
    fake_client.messages.create.return_value = response

    runner = CliRunner()
    with (
        _patch_news(),
        patch("auto_trading.cli._build_anthropic_client", return_value=fake_client),
    ):
        result = runner.invoke(
            app,
            [
                "weekly-proposal",
                "--fallback",
                str(fallback),
                "--current",
                str(isolated_cwd / "strategies" / "profiles" / "daytrade-current.yaml"),
            ],
        )

    assert result.exit_code == 0, result.output
    assert "top_n: 3 → 2" in result.output
    target = isolated_cwd / "strategies" / "profiles" / "daytrade-current.yaml"
    assert target.exists()
    loaded = DaytradeProfile.load_yaml(target)
    assert loaded.top_n == 2


def test_weekly_proposal_uses_news_only_path(
    isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """reports 없어도 news 가 있으면 진행 (collect_recent_news patch 로 1건 주입)."""
    _seed_default_profile(isolated_cwd)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    fake_news = [
        NewsItem(
            title="t",
            summary="s",
            url="https://x",
            published_at=datetime.now(KST),
            source="k",
            tickers=(),
        )
    ]

    block = MagicMock()
    block.type = "tool_use"
    block.name = "propose_daytrade_profile"
    block.input = {"summary_ko": "ok", "rationale_ko": "ok", "confidence": 0.5}
    response = MagicMock()
    response.content = [block]
    fake_client = MagicMock()
    fake_client.messages.create.return_value = response

    runner = CliRunner()
    with (
        _patch_news(fake_news),
        patch("auto_trading.cli._build_anthropic_client", return_value=fake_client),
    ):
        result = runner.invoke(app, ["weekly-proposal"])
    assert result.exit_code == 0, result.output


def test_weekly_proposal_max_news_items_flag_flows_to_propose(
    isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--max-news-items` 가 propose_daytrade_profile 호출에 그대로 전달."""
    _seed_default_profile(isolated_cwd)
    _seed_reports(isolated_cwd / "data" / "reports", ["2026-05-06"])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")

    block = MagicMock()
    block.type = "tool_use"
    block.name = "propose_daytrade_profile"
    block.input = {"summary_ko": "ok", "rationale_ko": "ok", "confidence": 0.5}
    response = MagicMock()
    response.content = [block]
    fake_client = MagicMock()
    fake_client.messages.create.return_value = response

    runner = CliRunner()
    with (
        _patch_news(),
        patch("auto_trading.cli._build_anthropic_client", return_value=fake_client),
    ):
        result = runner.invoke(app, ["weekly-proposal", "--max-news-items", "42"])

    assert result.exit_code == 0, result.output
    # CLI 출력에 "최대 42건" 표기 + LLM 호출이 한 번 발생.
    assert "최대 42건" in result.output
    assert fake_client.messages.create.call_count == 1


def test_fetch_news_outputs_count_and_sources(isolated_cwd: Path) -> None:
    """`auto-trade fetch-news` — collect_recent_news 결과를 콘솔에 표시 + 비용 0."""
    fake = [
        NewsItem(
            title="t1",
            summary="s",
            url="https://h.com/1",
            published_at=datetime.now(KST),
            source="한국경제",
            tickers=(),
        ),
        NewsItem(
            title="t2",
            summary="s",
            url="https://y.com/1",
            published_at=datetime.now(KST),
            source="연합뉴스",
            tickers=(),
        ),
        NewsItem(
            title="t3",
            summary="s",
            url="https://h.com/2",
            published_at=datetime.now(KST),
            source="한국경제",
            tickers=(),
        ),
    ]
    runner = CliRunner()
    with patch("auto_trading.news.collector.collect_recent_news", return_value=fake):
        result = runner.invoke(app, ["fetch-news"])
    assert result.exit_code == 0, result.output
    assert "뉴스 수집: 3건" in result.output
    assert "한국경제: 2건" in result.output
    assert "연합뉴스: 1건" in result.output
