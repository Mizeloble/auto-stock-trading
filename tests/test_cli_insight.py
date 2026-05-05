"""`auto-trade insight` 명령 회귀 — 데이터 부족 / dry-run / 정상 경로."""

from __future__ import annotations

import logging
from collections.abc import Generator
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import structlog
from typer.testing import CliRunner

from auto_trading.cli import app
from auto_trading.reports.daily import (
    DailyReport,
    LiquidationRecord,
    MarketGateInfo,
    TradeRecord,
    save_daily_report,
)


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
                market_gates=[
                    MarketGateInfo(market="KOSPI", last=1.0, sma5=1.0, sma20=1.0, above=True)
                ],
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
                error_count=0,
                note_ko=f"{d} 요약",
            ),
            root=root,
        )


def test_insight_no_reports_exits_with_hint(isolated_cwd: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["insight"])
    assert result.exit_code == 1
    assert "리포트 데이터 부족" in result.output


def test_insight_dry_run_skips_llm_and_prints_summary(isolated_cwd: Path) -> None:
    """--dry-run 은 Anthropic 키 없이도 동작 — 입력 요약만 콘솔에."""
    _seed_reports(isolated_cwd / "data" / "reports", ["2026-05-01", "2026-05-02", "2026-05-03"])
    runner = CliRunner()
    result = runner.invoke(app, ["insight", "--days", "3", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "입력: 3일" in result.output
    # 요약 dict 의 핵심 키가 노출.
    assert "days" in result.output
    assert "chosen_freq_top" in result.output


def test_insight_calls_anthropic_when_key_present(
    isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """키 있으면 Anthropic 클라이언트 빌드 → insight() 호출 → 결과 출력."""
    _seed_reports(isolated_cwd / "data" / "reports", ["2026-05-04", "2026-05-05"])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    # 캐싱된 settings 가 빈 키를 기억하지 않도록 lru_cache 무효화.
    from auto_trading.config import get_settings

    get_settings.cache_clear()

    fake_client = MagicMock()

    block = MagicMock()
    block.type = "tool_use"
    block.name = "propose_insight"
    block.input = {
        "summary_ko": "금주 안정 운영.",
        "key_findings": ["A 가 2회 연속 발굴"],
        "recommendations": ["top_n 유지"],
        "confidence": 0.7,
    }
    response = MagicMock()
    response.content = [block]
    fake_client.messages.create.return_value = response

    monkeypatch.setattr("auto_trading.cli._build_anthropic_client", lambda: fake_client)

    runner = CliRunner()
    result = runner.invoke(app, ["insight", "--days", "7"])
    assert result.exit_code == 0, result.output
    assert "🧭 주간 인사이트" in result.output
    assert "금주 안정 운영" in result.output
    assert "A 가 2회 연속 발굴" in result.output
    assert "신뢰도: 0.70" in result.output

    # cleanup — 다른 테스트가 cached settings 로 ANTHROPIC_API_KEY=test-key 를 보지 않게.
    get_settings.cache_clear()


def test_insight_default_root_uses_cwd(isolated_cwd: Path) -> None:
    """--reports-root 미지정 시 cwd/data/reports 를 본다."""
    _seed_reports(isolated_cwd / "data" / "reports", ["2026-05-05"])
    runner = CliRunner()
    result = runner.invoke(app, ["insight", "--days", "1", "--dry-run"])
    assert result.exit_code == 0
    assert "1일" in result.output


def test_insight_custom_reports_root(isolated_cwd: Path) -> None:
    custom = isolated_cwd / "elsewhere"
    _seed_reports(custom, ["2026-05-05"])
    runner = CliRunner()
    result = runner.invoke(
        app, ["insight", "--days", "1", "--reports-root", str(custom), "--dry-run"]
    )
    assert result.exit_code == 0
    assert "1일" in result.output


def test_insight_default_date_today_smoke(isolated_cwd: Path) -> None:
    """오늘 날짜로 리포트 1건 만들고 dry-run 호출해도 no-op 동작 확인."""
    today = datetime.now().date().isoformat()
    _seed_reports(isolated_cwd / "data" / "reports", [today])
    runner = CliRunner()
    result = runner.invoke(app, ["insight", "--dry-run"])
    assert result.exit_code == 0
