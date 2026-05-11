"""auto-trade preflight CLI 회귀.

29단계 PR8 (#17).

검증:
- 모든 활성 게이트 충족 → exit 0 + 안내 메시지.
- 한 게이트라도 실패 → exit 1 + ✗ 표시.
- 표 출력에 게이트 이름·상태·상세 포함.
- skipped 게이트는 별도로 표시.
- 미구현 게이트만 있고 활성 게이트 모두 통과 시 exit 0.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Generator
from pathlib import Path

import pytest
import structlog
from typer.testing import CliRunner

from auto_trading.cli import app


@pytest.fixture(autouse=True)
def _reset_structlog() -> Generator[None]:
    yield
    logging.getLogger().handlers.clear()
    structlog.reset_defaults()


def _setup_passing_env(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path, Path, Path]:
    """모든 활성 게이트가 통과하는 환경 — 5건 모두 ok + multi-channel 활성."""
    # 29단계 PR25 — multi_channel_alerts 게이트 통과 위해 두 채널 모두 활성.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot:fake")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/x/y")
    from auto_trading.config import get_settings

    get_settings.cache_clear()

    profile = root / "profile.yaml"
    profile.write_text(
        "risk:\n  stop_loss_pct: 0.01\n  daily_loss_kill: true\n",
        encoding="utf-8",
    )
    journal = root / "state.jsonl"  # 미존재 = "실행 이력 0" 통과
    heartbeat = root / "hb.txt"
    heartbeat.write_text("now", encoding="utf-8")
    reports = root / "reports"
    reports.mkdir()
    (reports / "daily-2026-05-01.json").write_text(
        json.dumps({"date": "2026-05-01", "realized_pnl_pct": -1.5}),
        encoding="utf-8",
    )
    backtests = root / "backtests"
    run_dir = backtests / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "report.json").write_text(json.dumps({"sharpe": 0.85}), encoding="utf-8")
    return profile, journal, heartbeat, reports, backtests


def _invoke(profile: Path, journal: Path, hb: Path, reports: Path, backtests: Path):
    return CliRunner().invoke(
        app,
        [
            "preflight",
            "--profile",
            str(profile),
            "--journal",
            str(journal),
            "--heartbeat",
            str(hb),
            "--reports-root",
            str(reports),
            "--backtests-root",
            str(backtests),
        ],
    )


def test_preflight_all_active_pass_returns_exit_0(
    isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile, journal, hb, reports, backtests = _setup_passing_env(isolated_cwd, monkeypatch)

    result = _invoke(profile, journal, hb, reports, backtests)

    assert result.exit_code == 0, result.output
    assert "모든 활성 게이트 통과" in result.output
    # 게이트 이름이 표에 노출.
    for name in (
        "risk_limits_active",
        "no_pending_orders",
        "heartbeat_recent",
        "recent_paper_day_mdd",
        "latest_backtest_sharpe",
    ):
        assert name in result.output


def test_preflight_one_gate_failure_returns_exit_1(
    isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile, journal, hb, reports, backtests = _setup_passing_env(isolated_cwd, monkeypatch)
    # 프로파일 의도적으로 망가뜨림 (kill_switch off).
    profile.write_text(
        "risk:\n  stop_loss_pct: 0.01\n  daily_loss_kill: false\n",
        encoding="utf-8",
    )

    result = _invoke(profile, journal, hb, reports, backtests)

    assert result.exit_code == 1, result.output
    assert "게이트 실패" in result.output
    assert "daily_loss_kill" in result.output


def test_preflight_shows_skipped_gates_section(
    isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """미구현 게이트는 별도 표시 (skip)."""
    profile, journal, hb, reports, backtests = _setup_passing_env(isolated_cwd, monkeypatch)

    result = _invoke(profile, journal, hb, reports, backtests)

    assert "kill_switch_simulation" in result.output
    # 29단계 PR25 — multi_channel_alerts 는 더 이상 placeholder 아님 (활성 게이트로 승격).
    assert "corporate_action_cache_fresh" in result.output
    assert "PR 미구현" in result.output


def test_preflight_falls_back_to_default_profile_if_current_missing(
    isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--profile 가 없는 경로면 default 로 fallback (운영 시 daytrade-current.yaml 가 없을 때)."""
    # _setup_passing_env 가 만든 profile 외에 default fallback 도 만들어둠.
    _profile, journal, hb, reports, backtests = _setup_passing_env(isolated_cwd, monkeypatch)
    default = isolated_cwd / "strategies" / "profiles" / "daytrade-default.yaml"
    default.parent.mkdir(parents=True, exist_ok=True)
    default.write_text(
        "risk:\n  stop_loss_pct: 0.01\n  daily_loss_kill: true\n",
        encoding="utf-8",
    )
    nonexistent = isolated_cwd / "missing.yaml"

    result = _invoke(nonexistent, journal, hb, reports, backtests)

    # fallback 안내 + default 가 통과 환경이라 exit 0.
    assert "fallback" in result.output
    assert result.exit_code == 0, result.output
