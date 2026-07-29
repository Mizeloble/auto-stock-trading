"""CLI `paper-day` 회귀 테스트.

검증:
- typer CliRunner 로 호출 시 PaperRunner 가 day-trade 모드(liquidate_at) 로 생성되는지
- --liquidate 잘못된 형식 → BadParameter
- delayed_discover 가 정상적으로 discover_and_arm 호출 + 결과 반영
- delayed_discover 가 발굴 실패 시 runner.request_stop 호출 (러너 hang 방지)
- swing 모드 `paper` 명령은 기존 동작 그대로 (회귀)
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import date, time
from unittest.mock import AsyncMock, patch

import pytest
import structlog
from typer.testing import CliRunner

from auto_trading import cli as cli_module
from auto_trading._common import KST
from auto_trading.daytrade import cli_helpers
from auto_trading.daytrade.orchestrator import DiscoveryResult
from auto_trading.runner import PaperRunner


@pytest.fixture(autouse=True)
def _restore_structlog_config() -> Iterator[None]:
    """CliRunner.invoke 가 cli._root 콜백에서 _setup_logging() 호출 → structlog 전역
    재구성. test_notify_telegram 등 다른 테스트로 누수 방지."""
    yield
    structlog.reset_defaults()


@pytest.fixture(autouse=True)
def _force_weekday(monkeypatch: pytest.MonkeyPatch) -> None:
    """기존 paper/paper-day 테스트는 휴장일 가드 도입 전에 작성 — 토/일/공휴일에
    돌리면 가드가 먼저 발화해 PaperRunner 가 안 만들어진다. 평일 고정으로 회귀 격리.
    `test_cli_holiday_guard.py` 가 가드 로직 자체를 검증."""
    monkeypatch.setattr(cli_module.krx_calendar, "today_kst", lambda: date(2026, 5, 8))


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
        patch.object(cli_module, "paper_day_main", AsyncMock(side_effect=_no_op)),
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


def test_paper_day_with_profile_overrides_cli_args(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """`--profile` 지정 시 top_n/liquidate/risk 가 프로파일 값으로 덮어써진다."""
    from auto_trading.daytrade.profile import (
        DaytradeProfile,
        DaytradeRiskLimits,
        DaytradeStrategyParams,
    )

    profile = DaytradeProfile(
        name="t",
        strategy="index_momentum_daytrade",
        top_n=2,
        liquidate="14:50",
        params=DaytradeStrategyParams(cooldown_min=15, momentum_threshold_pct=0.4),
        risk=DaytradeRiskLimits(stop_loss_pct=0.012, daily_loss_kill=True),
    )
    yaml_path = tmp_path / "p.yaml"
    profile.dump_yaml(yaml_path)

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
        patch.object(cli_module, "paper_day_main", AsyncMock(side_effect=_no_op)),
        patch.object(cli_module, "build_command_handler", return_value=None),
    ):
        # CLI 인자는 top_n=5/liquidate=15:00 인데 profile 이 우선해야.
        result = runner.invoke(
            cli_module.app,
            [
                "paper-day",
                "--top-n",
                "5",
                "--liquidate",
                "15:00",
                "--dry-run",
                "--profile",
                str(yaml_path),
            ],
        )

    assert result.exit_code == 0, result.output
    r = runner_holder["r"]
    assert r.liquidate_at == time(14, 50, tzinfo=KST)  # profile 우선
    assert r.risk.limits.stop_loss_pct == 0.012
    assert r.risk.limits.daily_loss_kill is True


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


async def test_delayed_discover_calls_orchestrator_and_pushes_notify(isolated_cwd) -> None:
    """delayed_discover 가 discover_and_arm 결과를 notifier 로 푸시."""
    fake_notifier = AsyncMock()
    runner = PaperRunner(liquidate_at=time(15, 15, tzinfo=KST), notifier=fake_notifier)

    fake_result = DiscoveryResult(
        chosen=[],
        blocked=[],
        gates={},
        note_ko="테스트 결과",
    )
    with (
        patch.object(cli_helpers, "discover_and_arm", AsyncMock(return_value=fake_result)),
        patch.object(cli_helpers, "KiwoomRestClient") as mock_rest_cls,
    ):
        # async context manager mock
        mock_rest_cls.return_value.__aenter__ = AsyncMock(return_value=object())
        mock_rest_cls.return_value.__aexit__ = AsyncMock(return_value=None)

        await cli_helpers.delayed_discover(runner, top_n=3, delay=0)

    events = [c.args[0] for c in fake_notifier.notify.call_args_list]
    assert "daytrade.discovery_done" in events


async def test_delayed_discover_failure_requests_stop(isolated_cwd) -> None:
    """발굴 실패 → runner.request_stop 호출 (universe 대기 중인 러너 깨움)."""
    runner = PaperRunner(liquidate_at=time(15, 15, tzinfo=KST))

    with (
        patch.object(
            cli_helpers, "discover_and_arm", AsyncMock(side_effect=RuntimeError("api down"))
        ),
        patch.object(cli_helpers, "KiwoomRestClient") as mock_rest_cls,
    ):
        mock_rest_cls.return_value.__aenter__ = AsyncMock(return_value=object())
        mock_rest_cls.return_value.__aexit__ = AsyncMock(return_value=None)

        await cli_helpers.delayed_discover(runner, top_n=3, delay=0)

    # request_stop 호출 → STOPPING 진입 + universe_ready 도 set
    assert runner._control_state.value == "stopping"
    assert runner._universe_ready.is_set()


async def test_delayed_discover_failure_pushes_alert(isolated_cwd) -> None:
    """발굴 실패는 로그로 끝내지 않고 즉시 푸시한다.

    2026-07-27~29: 토큰 만료로 3거래일간 매매 0 이었는데 알림이 한 통도 없었다.
    """
    fake_notifier = AsyncMock()
    runner = PaperRunner(liquidate_at=time(15, 15, tzinfo=KST), notifier=fake_notifier)

    with (
        patch.object(
            cli_helpers, "discover_and_arm", AsyncMock(side_effect=RuntimeError("인증에 실패"))
        ),
        patch.object(cli_helpers, "KiwoomRestClient") as mock_rest_cls,
    ):
        mock_rest_cls.return_value.__aenter__ = AsyncMock(return_value=object())
        mock_rest_cls.return_value.__aexit__ = AsyncMock(return_value=None)

        await cli_helpers.delayed_discover(runner, top_n=3, delay=0)

    calls = {c.args[0]: c.kwargs for c in fake_notifier.notify.call_args_list}
    assert "daytrade.discover_failed" in calls
    assert "인증에 실패" in str(calls["daytrade.discover_failed"].get("error"))


async def test_delayed_discover_empty_chosen_requests_stop(isolated_cwd) -> None:
    """5/3 21:42 회귀 — 게이트 차단으로 chosen=[] 일 때도 hang 방지 위해 request_stop.

    무엇/왜:
    - discover_and_arm 가 chosen=[] 면 set_universe() 호출 안 함 → _universe_ready
      안 set → run() 의 universe 대기에서 무한 hang.
    - 청산 시계 task 는 ws.start 이후에 시작되므로 universe 대기 중엔 안 돔 →
      청산 시각 지나도 자기 종료 X.
    - 발굴 실패와 동일하게 명시적 stop 트리거 필요.
    """
    fake_notifier = AsyncMock()
    runner = PaperRunner(liquidate_at=time(15, 15, tzinfo=KST), notifier=fake_notifier)
    empty_result = DiscoveryResult(
        chosen=[],
        blocked=[("006340", "KOSPI_below_sma")],
        gates={},
        note_ko="후보 1건 모두 지수 게이트 차단",
    )
    with (
        patch.object(cli_helpers, "discover_and_arm", AsyncMock(return_value=empty_result)),
        patch.object(cli_helpers, "KiwoomRestClient") as mock_rest_cls,
    ):
        mock_rest_cls.return_value.__aenter__ = AsyncMock(return_value=object())
        mock_rest_cls.return_value.__aexit__ = AsyncMock(return_value=None)

        await cli_helpers.delayed_discover(runner, top_n=3, delay=0)

    assert runner._control_state.value == "stopping", (
        "chosen=[] 시 STOPPING 진입 누락 (5/3 hang 회귀)"
    )
    assert runner._universe_ready.is_set()
    # discovery_done 알림은 stop 전에 푸시돼야 (사용자가 결과 알 수 있게)
    events = [c.args[0] for c in fake_notifier.notify.call_args_list]
    assert "daytrade.discovery_done" in events


async def test_delayed_discover_chosen_keeps_runner_alive(isolated_cwd) -> None:
    """chosen 이 있으면 set_universe 가 호출되고 stop 트리거 X — 정상 매수 흐름."""
    from auto_trading.market.rank import RankedSymbol

    runner = PaperRunner(liquidate_at=time(15, 15, tzinfo=KST), notifier=AsyncMock())
    chosen = [
        RankedSymbol(symbol="005930", market="KOSPI", name="삼성전자", volume=100, price=70000)
    ]
    result = DiscoveryResult(chosen=chosen, blocked=[], gates={}, note_ko="ok")

    async def _fake_discover(_rest, runner_arg, **_kw):  # type: ignore[no-untyped-def]
        # 실 orchestrator 처럼 set_universe 호출 (실측 동작 시뮬)
        from auto_trading.strategy.intraday import IntradayMomentum

        runner_arg.set_universe(["005930"], {"005930": IntradayMomentum()})
        return result

    with (
        patch.object(cli_helpers, "discover_and_arm", _fake_discover),
        patch.object(cli_helpers, "KiwoomRestClient") as mock_rest_cls,
    ):
        mock_rest_cls.return_value.__aenter__ = AsyncMock(return_value=object())
        mock_rest_cls.return_value.__aexit__ = AsyncMock(return_value=None)

        await cli_helpers.delayed_discover(runner, top_n=3, delay=0)

    assert runner._control_state.value == "running", "chosen 있는데 STOPPING 진입 — 회귀"
    assert runner.symbols == ["005930"]


async def test_delayed_discover_respects_delay(isolated_cwd) -> None:
    """delay > 0 일 때 sleep 이 실제로 실행됨 (asyncio.sleep mock)."""
    runner = PaperRunner(liquidate_at=time(15, 15, tzinfo=KST))
    fake_result = DiscoveryResult(chosen=[], blocked=[], gates={}, note_ko="x")

    sleep_calls: list[float] = []

    async def _track_sleep(sec):  # type: ignore[no-untyped-def]
        sleep_calls.append(sec)

    with (
        patch.object(asyncio, "sleep", _track_sleep),
        patch.object(cli_helpers, "discover_and_arm", AsyncMock(return_value=fake_result)),
        patch.object(cli_helpers, "KiwoomRestClient") as mock_rest_cls,
    ):
        mock_rest_cls.return_value.__aenter__ = AsyncMock(return_value=object())
        mock_rest_cls.return_value.__aexit__ = AsyncMock(return_value=None)

        await cli_helpers.delayed_discover(runner, top_n=3, delay=10)

    assert 10 in sleep_calls
