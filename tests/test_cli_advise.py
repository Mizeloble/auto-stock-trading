"""`auto-trade strategy advise` CLI 테스트 — Anthropic SDK / 차트 모두 stub."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import structlog
from typer.testing import CliRunner

from auto_trading import cli
from auto_trading._common import Bar
from auto_trading.config import Settings, get_settings
from auto_trading.strategy import llm_advisor as advisor
from auto_trading.strategy.profile import StrategyProfile

runner = CliRunner()


@pytest.fixture(autouse=True)
def _restore_structlog_config() -> Iterator[None]:
    yield
    structlog.reset_defaults()


def _bars() -> list[Bar]:
    return [
        Bar(
            symbol="005930",
            ts=datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=i),
            open=100.0 + i,
            high=101.0 + i,
            low=99.0 + i,
            close=100.0 + i,
            volume=1,
        )
        for i in range(30)
    ]


def _profile_path(tmp_path: Path) -> Path:
    profile = StrategyProfile(
        name="auto_005930",
        symbol="005930",
        strategy="rsi_reversion",
        params={"period": 14, "oversold": 30, "overbought": 70},
        weight=1.0,
        notes="기존",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    p = tmp_path / "p.yaml"
    profile.dump_yaml(p)
    return p


def _patch_chart_and_client(
    monkeypatch: pytest.MonkeyPatch, advisor_response: advisor.AdvisorResponse
) -> None:
    async def _fake_get_daily_bars(*args: object, **kwargs: object) -> list[Bar]:
        return _bars()

    monkeypatch.setattr(cli, "get_daily_bars", _fake_get_daily_bars)
    monkeypatch.setattr(cli, "_build_anthropic_client", lambda: object())
    monkeypatch.setattr(advisor, "advise", lambda *a, **kw: advisor_response)


def test_advise_dry_run_does_not_modify_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = advisor.AdvisorResponse(
        change=advisor.AdvisorChange(
            strategy="sma_crossover",
            params={"short": 5, "long": 20},
            weight=0.8,
        ),
        reason_ko="추세 전환",
        confidence=0.7,
    )
    _patch_chart_and_client(monkeypatch, response)
    path = _profile_path(tmp_path)
    before = path.read_bytes()

    result = runner.invoke(cli.app, ["strategy", "advise", "--profile", str(path), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output
    assert path.read_bytes() == before  # 수정 안 됨
    assert not path.with_suffix(path.suffix + ".bak").exists()  # 백업도 없음


def test_advise_writes_backup_and_updates_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = advisor.AdvisorResponse(
        change=advisor.AdvisorChange(
            strategy="sma_crossover",
            params={"short": 5, "long": 20},
            weight=0.8,
        ),
        reason_ko="추세 전환",
        confidence=0.7,
    )
    _patch_chart_and_client(monkeypatch, response)
    path = _profile_path(tmp_path)

    result = runner.invoke(cli.app, ["strategy", "advise", "--profile", str(path)])
    assert result.exit_code == 0, result.output

    backup = path.with_suffix(path.suffix + ".bak")
    assert backup.exists()
    updated = StrategyProfile.load_yaml(path)
    assert updated.strategy == "sma_crossover"
    assert updated.params == {"short": 5, "long": 20}
    assert updated.weight == 0.8
    assert updated.last_advised_at is not None
    assert "추세 전환" in updated.notes


def test_advise_no_change_skips_backup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    response = advisor.AdvisorResponse(
        change=advisor.AdvisorChange(
            strategy="rsi_reversion",
            params={"period": 14, "oversold": 30, "overbought": 70},
            weight=1.0,
        ),
        reason_ko="현행 유지",
        confidence=0.5,
    )
    _patch_chart_and_client(monkeypatch, response)
    path = _profile_path(tmp_path)

    result = runner.invoke(cli.app, ["strategy", "advise", "--profile", str(path)])
    assert result.exit_code == 0, result.output
    assert "변경 없음" in result.output
    assert not path.with_suffix(path.suffix + ".bak").exists()


def test_advise_without_api_key_exits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # get_settings 가 캐시되어 있으므로 cache 우회 + 빈 키 Settings 주입
    get_settings.cache_clear()
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: Settings(_env_file=None, anthropic_api_key=""),  # type: ignore[arg-type]
    )

    async def _fake_get_daily_bars(*args: object, **kwargs: object) -> list[Bar]:
        return _bars()

    monkeypatch.setattr(cli, "get_daily_bars", _fake_get_daily_bars)
    path = _profile_path(tmp_path)

    result = runner.invoke(cli.app, ["strategy", "advise", "--profile", str(path)])
    assert result.exit_code != 0
    assert "ANTHROPIC_API_KEY" in result.output
    get_settings.cache_clear()


def _consume_iter(_: Any) -> None:
    """unused_argument 마커."""
