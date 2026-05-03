"""CLI `paper-day` 회귀 테스트.

검증:
- typer CliRunner 로 호출 시 PaperRunner 가 day-trade 모드(liquidate_at) 로 생성되는지
- --liquidate 잘못된 형식 → BadParameter
- _delayed_discover 가 정상적으로 discover_and_arm 호출 + 결과 반영
- _delayed_discover 가 발굴 실패 시 runner.request_stop 호출 (러너 hang 방지)
- swing 모드 `paper` 명령은 기존 동작 그대로 (회귀)
"""

from __future__ import annotations

import asyncio
from datetime import time
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from auto_trading import cli as cli_module
from auto_trading._common import KST
from auto_trading.daytrade.orchestrator import DiscoveryResult
from auto_trading.runner import PaperRunner


def test_parse_hhmm_kst_valid() -> None:
    t = cli_module._parse_hhmm_kst("15:15")
    assert t == time(15, 15, tzinfo=KST)


def test_parse_hhmm_kst_invalid_raises_bad_parameter() -> None:
    import typer

    with pytest.raises(typer.BadParameter):
        cli_module._parse_hhmm_kst("3pm")
    with pytest.raises(typer.BadParameter):
        cli_module._parse_hhmm_kst("25:99")  # ValueError 안에서 wrap


def test_paper_day_constructs_runner_in_day_trade_mode() -> None:
    """`paper-day --dry-run` 호출 → PaperRunner(liquidate_at=15:15, dry_run=True) 생성."""
    runner_holder: dict[str, PaperRunner] = {}

    real_init = PaperRunner.__init__

    def _capture_init(self, **kwargs):  # type: ignore[no-untyped-def]
        real_init(self, **kwargs)
        runner_holder["r"] = self

    async def _no_op(*_a, **_kw):  # type: ignore[no-untyped-def]
        return None

    runner = CliRunner()
    with (
        patch.object(PaperRunner, "__init__", _capture_init),
        patch.object(cli_module, "_paper_day_main", AsyncMock(side_effect=_no_op)),
        patch.object(cli_module, "build_command_handler", return_value=None),
    ):
        result = runner.invoke(
            cli_module.app, ["paper-day", "--top-n", "5", "--liquidate", "15:00", "--dry-run"]
        )

    assert result.exit_code == 0, result.output
    r = runner_holder["r"]
    assert r.liquidate_at == time(15, 0, tzinfo=KST)
    assert r.dry_run is True
    assert r.symbols == []  # day-trade 모드는 시작 시 빈 universe


def test_paper_day_invalid_liquidate_format_fails() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_module.app, ["paper-day", "--liquidate", "afternoon"])
    assert result.exit_code != 0
    assert "HH:MM" in result.output or "Invalid" in result.output


def test_paper_command_swing_mode_unchanged() -> None:
    """기존 paper 명령에 --profile 안 주고 --strategy/--symbols 만 주면 정상 진입.

    회귀 게이트 — paper-day 추가가 paper 의 기존 진입 로직을 깨지 않는지.
    """
    runner_holder: dict[str, PaperRunner] = {}
    real_init = PaperRunner.__init__

    def _capture_init(self, **kwargs):  # type: ignore[no-untyped-def]
        real_init(self, **kwargs)
        runner_holder["r"] = self

    async def _no_op(*_a, **_kw):  # type: ignore[no-untyped-def]
        return None

    runner = CliRunner()
    with (
        patch.object(PaperRunner, "__init__", _capture_init),
        patch.object(PaperRunner, "run", AsyncMock(side_effect=_no_op)),
        patch.object(cli_module, "build_command_handler", return_value=None),
    ):
        result = runner.invoke(
            cli_module.app,
            ["paper", "--strategy", "sma_crossover", "--symbols", "005930", "--dry-run"],
        )

    assert result.exit_code == 0, result.output
    r = runner_holder["r"]
    assert r.liquidate_at is None  # swing 모드 — 청산 시각 없음
    assert r.symbols == ["005930"]
    assert r.dry_run is True


async def test_delayed_discover_calls_orchestrator_and_pushes_notify() -> None:
    """_delayed_discover 가 discover_and_arm 결과를 notifier 로 푸시."""
    fake_notifier = AsyncMock()
    runner = PaperRunner(liquidate_at=time(15, 15, tzinfo=KST), notifier=fake_notifier)

    fake_result = DiscoveryResult(
        chosen=[],
        blocked=[],
        gates={},
        note_ko="테스트 결과",
    )
    with (
        patch.object(cli_module, "discover_and_arm", AsyncMock(return_value=fake_result)),
        patch.object(cli_module, "KiwoomRestClient") as mock_rest_cls,
    ):
        # async context manager mock
        mock_rest_cls.return_value.__aenter__ = AsyncMock(return_value=object())
        mock_rest_cls.return_value.__aexit__ = AsyncMock(return_value=None)

        await cli_module._delayed_discover(runner, top_n=3, delay=0)

    events = [c.args[0] for c in fake_notifier.notify.call_args_list]
    assert "daytrade.discovery_done" in events


async def test_delayed_discover_failure_requests_stop() -> None:
    """발굴 실패 → runner.request_stop 호출 (universe 대기 중인 러너 깨움)."""
    runner = PaperRunner(liquidate_at=time(15, 15, tzinfo=KST))

    with (
        patch.object(
            cli_module, "discover_and_arm", AsyncMock(side_effect=RuntimeError("api down"))
        ),
        patch.object(cli_module, "KiwoomRestClient") as mock_rest_cls,
    ):
        mock_rest_cls.return_value.__aenter__ = AsyncMock(return_value=object())
        mock_rest_cls.return_value.__aexit__ = AsyncMock(return_value=None)

        await cli_module._delayed_discover(runner, top_n=3, delay=0)

    # request_stop 호출 → STOPPING 진입 + universe_ready 도 set
    assert runner._control_state.value == "stopping"
    assert runner._universe_ready.is_set()


async def test_delayed_discover_respects_delay() -> None:
    """delay > 0 일 때 sleep 이 실제로 실행됨 (asyncio.sleep mock)."""
    runner = PaperRunner(liquidate_at=time(15, 15, tzinfo=KST))
    fake_result = DiscoveryResult(chosen=[], blocked=[], gates={}, note_ko="x")

    sleep_calls: list[float] = []

    async def _track_sleep(sec):  # type: ignore[no-untyped-def]
        sleep_calls.append(sec)

    with (
        patch.object(asyncio, "sleep", _track_sleep),
        patch.object(cli_module, "discover_and_arm", AsyncMock(return_value=fake_result)),
        patch.object(cli_module, "KiwoomRestClient") as mock_rest_cls,
    ):
        mock_rest_cls.return_value.__aenter__ = AsyncMock(return_value=object())
        mock_rest_cls.return_value.__aexit__ = AsyncMock(return_value=None)

        await cli_module._delayed_discover(runner, top_n=3, delay=10)

    assert 10 in sleep_calls
