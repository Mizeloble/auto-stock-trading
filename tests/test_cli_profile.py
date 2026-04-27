"""CLI 의 `--profile` 옵션이 YAML 을 읽어 backtest 를 실행하는지."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import structlog
from typer.testing import CliRunner

from auto_trading import cli
from auto_trading._common import Bar
from auto_trading.strategy.profile import StrategyProfile

runner = CliRunner()

# CI(no-TTY)와 로컬(TTY) 모두에서 안정적으로 substring 매칭하기 위해 ANSI/박스
# 문자/개행을 모두 평문으로 정규화. Rich 가 토큰 사이에 ANSI escape 를 끼워
# 넣어 `--profile` 같은 substring 이 안 걸리는 경우를 방지.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_BOX_RE = re.compile(r"[│╭╮╰╯─┌┐└┘├┤┬┴┼]+")


def _plain(output: str) -> str:
    return _BOX_RE.sub(" ", _ANSI_RE.sub("", output)).replace("\n", " ")


@pytest.fixture(autouse=True)
def _restore_structlog_config() -> Iterator[None]:
    """cli._setup_logging 가 structlog 를 전역 재구성하므로,
    테스트 후 기본값으로 되돌려 다른 테스트(특히 test_notify_telegram)에 누수 방지."""
    yield
    structlog.reset_defaults()


def _fake_bars(symbol: str = "005930", n: int = 30) -> list[Bar]:
    return [
        Bar(
            symbol=symbol,
            ts=datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=i),
            open=100.0 + i,
            high=101.0 + i,
            low=99.0 + i,
            close=100.0 + i,
            volume=1000,
        )
        for i in range(n)
    ]


@pytest.fixture
def patched_chart(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_get_daily_bars(*args: object, **kwargs: object) -> list[Bar]:
        return _fake_bars()

    monkeypatch.setattr(cli, "get_daily_bars", _fake_get_daily_bars)


def test_backtest_with_profile_loads_yaml(
    tmp_path: Path,
    patched_chart: None,
) -> None:
    profile = StrategyProfile(
        name="auto_005930",
        symbol="005930",
        strategy="sma_crossover",
        params={"short": 3, "long": 5},
        weight=1.0,
        notes="테스트",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    profile_path = tmp_path / "p.yaml"
    profile.dump_yaml(profile_path)
    out_dir = tmp_path / "bt"

    result = runner.invoke(
        cli.app,
        [
            "backtest",
            "--profile",
            str(profile_path),
            "--from",
            "2025-01-01",
            "--out",
            str(out_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "프로파일 로드" in result.output
    assert (out_dir / "report.json").exists()


def test_backtest_rejects_profile_with_strategy_combo(tmp_path: Path) -> None:
    profile_path = tmp_path / "p.yaml"
    profile_path.write_text(
        "name: x\nsymbol: '005930'\nstrategy: sma_crossover\n", encoding="utf-8"
    )
    result = runner.invoke(
        cli.app,
        [
            "backtest",
            "--profile",
            str(profile_path),
            "--strategy",
            "sma_crossover",
            "--symbol",
            "005930",
            "--from",
            "2025-01-01",
        ],
    )
    assert result.exit_code != 0
    plain = _plain(result.output)
    assert "동시에" in plain and "줄 수 없습니다" in plain


def test_backtest_requires_one_of_profile_or_strategy() -> None:
    result = runner.invoke(cli.app, ["backtest", "--from", "2025-01-01"])
    assert result.exit_code != 0
    plain = _plain(result.output)
    # 핵심: 옵션 누락에 대한 BadParameter 가 떴는지 확인.
    # 한국어 메시지는 패널 폭에 따라 wrap 되므로 짧은 토큰만 검사.
    assert "--profile" in plain
    assert "필요합니다" in plain
