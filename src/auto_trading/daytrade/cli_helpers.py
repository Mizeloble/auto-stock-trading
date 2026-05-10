"""paper-day CLI 의 비-typer 비동기 글루.

cli.py 의 `paper-day` 명령에서만 호출되는 백그라운드 태스크 조합.
- `paper_day_main` / `paper_day_with_commands` — 러너 시동 + (옵션) 텔레그램 명령
  핸들러 동시 폴링 + 발굴 task 생명주기 관리.
- `delayed_discover` — 러너 ws.start 안정화 대기 후 `discover_and_arm` 호출 +
  결과 알림 + universe 0 건 시 명시적 stop 트리거(러너 hang 방지).
- `build_profile_factory` / `apply_profile_to_runner` — 23단계 Phase G,
  `DaytradeProfile` 을 strategy_factory + RiskLimits 에 적용.

여기에 typer 명령 정의는 두지 않는다 — typer 의존은 cli.py 에 격리.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path

import structlog
from rich.console import Console

from ..kiwoom.rest import KiwoomRestClient
from ..market.index import IndexMomentumTracker, Market
from ..notify.factory import build_notifier
from ..notify.reporting import ReportingNotifier
from ..notify.telegram_commands import TelegramCommandHandler
from ..notify.throttle import ThrottledNotifier
from ..runner import PaperRunner
from ..strategy.base import Strategy
from ..strategy.index_daytrade import IndexMomentumDayTrade
from ..strategy.intraday import IntradayMomentum
from .orchestrator import discover_and_arm, start_momentum_loop
from .profile import DaytradeProfile

console = Console()


@dataclass(frozen=True)
class PaperDayOverrides:
    """프로파일 적용 후 정착된 paper-day 설정값.

    프로파일 미지정 시 CLI 인자가 그대로 통과. 프로파일 있으면 해당 필드만 덮어씀.
    `summary_msg` 는 콘솔 출력용 (없으면 None).
    """

    profile: DaytradeProfile | None
    top_n: int
    liquidate: str
    discover_delay_sec: int
    include_etf: bool
    summary_msg: str | None


def apply_daytrade_profile_overrides(
    profile_path: Path | None,
    *,
    top_n: int,
    liquidate: str,
    discover_delay_sec: int,
    include_etf: bool,
) -> PaperDayOverrides:
    """프로파일 경로가 있으면 YAML 로드해 strategy/top_n/liquidate/discover_delay/exclude_etf
    를 덮어쓴 결과를 반환. 없으면 CLI 인자 그대로 통과.

    side-effect 없음 — 호출자가 `summary_msg` 를 console 에 출력할지 결정.
    """
    if profile_path is None:
        return PaperDayOverrides(
            profile=None,
            top_n=top_n,
            liquidate=liquidate,
            discover_delay_sec=discover_delay_sec,
            include_etf=include_etf,
            summary_msg=None,
        )
    profile = DaytradeProfile.load_yaml(profile_path)
    return PaperDayOverrides(
        profile=profile,
        top_n=profile.top_n,
        liquidate=profile.liquidate,
        discover_delay_sec=profile.discover_delay_sec,
        include_etf=not profile.exclude_etf,
        summary_msg=(
            f"프로파일 로드: {profile_path.name} "
            f"({profile.strategy} · top_n={profile.top_n} · 청산 {profile.liquidate})"
        ),
    )


def build_paper_day_runner(
    overrides: PaperDayOverrides,
    *,
    liquidate_at: time,
    bar_minutes: int,
    allow_real: bool,
    dry_run: bool,
    telegram_throttle_min: int,
    today_kst: date,
    log_dir: Path = Path("logs"),
    report_root: Path = Path("data/reports"),
) -> tuple[PaperRunner, IndexMomentumTracker | None]:
    """notifier 체인 + PaperRunner + ReportingNotifier wrap + (옵션) momentum tracker 까지 한 번에.

    반환:
    - runner — `runner.notifier` 가 ReportingNotifier 로 감싸진 상태.
    - momentum_tracker — `index_momentum_daytrade` 프로파일에서만 비-None.

    호출자는 (필요 시) command handler 만 별도 wiring 후 paper_day_main / paper_day_with_commands
    중 하나를 await 하면 된다.
    """
    inner = build_notifier()
    if telegram_throttle_min > 0:
        inner = ThrottledNotifier(inner, window_min=telegram_throttle_min)

    runner = PaperRunner(
        liquidate_at=liquidate_at,
        bar_minutes=bar_minutes,
        allow_real=allow_real,
        dry_run=dry_run,
        notifier=inner,
    )

    momentum_tracker: IndexMomentumTracker | None = None
    if overrides.profile is not None:
        apply_profile_to_runner(overrides.profile, runner)
        if overrides.profile.strategy == "index_momentum_daytrade":
            momentum_tracker = IndexMomentumTracker()

    runner.notifier = ReportingNotifier(
        inner,
        state_provider=runner.state_snapshot,
        log_path=log_dir / f"paper-day-{today_kst.isoformat()}.log",
        report_root=report_root,
        report_date=today_kst,
    )

    return runner, momentum_tracker


def apply_profile_to_runner(profile: DaytradeProfile, runner: PaperRunner) -> None:
    """프로파일의 RiskLimits 를 러너의 risk.limits 에 in-place 반영.

    PositionGuard 가 같은 limits 객체를 참조하므로 stop/trailing/partial 도 자동 활성.
    """
    limits = runner.risk.limits
    risk = profile.risk
    limits.stop_loss_pct = risk.stop_loss_pct
    limits.trailing_stop_pct = risk.trailing_stop_pct
    limits.partial_take_pct = risk.partial_take_pct
    limits.partial_take_qty_ratio = risk.partial_take_qty_ratio
    limits.max_order_value = risk.max_order_value
    limits.daily_notional_limit = risk.daily_notional_limit
    limits.daily_loss_kill = risk.daily_loss_kill


def build_profile_factory(
    profile: DaytradeProfile,
    tracker: IndexMomentumTracker,
) -> Callable[..., Strategy]:
    """프로파일 → strategy_factory. discover_and_arm 이 (sym, market=...) 으로 호출."""
    weight = 1.0 / max(1, profile.top_n)

    if profile.strategy == "intraday_momentum":

        def _intraday(_symbol: str, *, market: Market | None = None) -> Strategy:
            del market  # 미사용
            return IntradayMomentum(weight=weight)

        return _intraday

    # index_momentum_daytrade — Phase D + F
    def _index(_symbol: str, *, market: Market | None = None) -> Strategy:
        return IndexMomentumDayTrade(
            weight=weight,
            cooldown_min=profile.params.cooldown_min,
            momentum_lookback_min=profile.params.momentum_lookback_min,
            momentum_threshold_pct=profile.params.momentum_threshold_pct,
            momentum_tracker=tracker,
            market=market,
        )

    return _index


async def _momentum_runner(
    tracker: IndexMomentumTracker,
    *,
    markets: tuple[Market, ...],
    interval_sec: int,
    lookback_min: int,
) -> None:
    """별도 KiwoomRestClient 컨텍스트로 momentum loop 시동. cancel 시 client close."""
    async with KiwoomRestClient() as rest:
        task = start_momentum_loop(
            rest,
            tracker,
            markets=markets,
            interval_sec=interval_sec,
            lookback_min=lookback_min,
        )
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def paper_day_main(
    runner: PaperRunner,
    *,
    top_n: int,
    discover_delay: int,
    exclude_etf: bool,
    profile: DaytradeProfile | None = None,
    momentum_tracker: IndexMomentumTracker | None = None,
) -> None:
    """러너를 백그라운드로 시동 + discover_and_arm 으로 universe 주입.

    profile 이 있으면 strategy_factory 와 momentum_loop 를 함께 시동.
    """
    factory = (
        build_profile_factory(profile, momentum_tracker) if profile and momentum_tracker else None
    )
    runner_task = asyncio.create_task(runner.run(), name="paper-day-runner")
    discover_task = asyncio.create_task(
        delayed_discover(
            runner,
            top_n=top_n,
            delay=discover_delay,
            exclude_etf=exclude_etf,
            strategy_factory=factory,
        ),
        name="day-discover",
    )
    momentum_task = _spawn_momentum_task(profile, momentum_tracker)
    try:
        await runner_task  # 청산 시각 도달 후 자기 종료
    finally:
        await _cancel_aux(discover_task, momentum_task)


async def paper_day_with_commands(
    runner: PaperRunner,
    handler: TelegramCommandHandler,
    *,
    top_n: int,
    discover_delay: int,
    exclude_etf: bool,
    profile: DaytradeProfile | None = None,
    momentum_tracker: IndexMomentumTracker | None = None,
) -> None:
    """텔레그램 명령 동시 폴링 버전."""
    factory = (
        build_profile_factory(profile, momentum_tracker) if profile and momentum_tracker else None
    )
    runner_task = asyncio.create_task(runner.run(), name="paper-day-runner")
    cmd_task = asyncio.create_task(handler.run(), name="tg-commands")
    discover_task = asyncio.create_task(
        delayed_discover(
            runner,
            top_n=top_n,
            delay=discover_delay,
            exclude_etf=exclude_etf,
            strategy_factory=factory,
        ),
        name="day-discover",
    )
    momentum_task = _spawn_momentum_task(profile, momentum_tracker)
    try:
        done, pending = await asyncio.wait(
            {runner_task, cmd_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        for t in pending:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        for t in done:
            t.result()
    finally:
        await _cancel_aux(discover_task, momentum_task)
        await handler.aclose()


def _spawn_momentum_task(
    profile: DaytradeProfile | None,
    tracker: IndexMomentumTracker | None,
) -> asyncio.Task[None] | None:
    """게이트 활성 + tracker 가용 시 background momentum loop 시동."""
    if profile is None or tracker is None:
        return None
    if profile.strategy != "index_momentum_daytrade":
        return None
    if profile.params.momentum_threshold_pct <= 0:
        return None
    return asyncio.create_task(
        _momentum_runner(
            tracker,
            markets=("KOSPI", "KOSDAQ"),
            interval_sec=60,
            lookback_min=profile.params.momentum_lookback_min,
        ),
        name="momentum-runner",
    )


async def _cancel_aux(*tasks: asyncio.Task[None] | None) -> None:
    for t in tasks:
        if t is None:
            continue
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await t


async def delayed_discover(
    runner: PaperRunner,
    *,
    top_n: int,
    delay: int,
    exclude_etf: bool = True,
    strategy_factory: Callable[..., Strategy] | None = None,
) -> None:
    """러너 시동 후 `delay` 초 대기 → discover_and_arm.

    실패해도 러너 자체는 청산 시계로 종료될 수 있게 예외를 swallow + 로그.
    """
    log = structlog.get_logger("daytrade.cli")
    if delay > 0:
        await asyncio.sleep(delay)
    try:
        async with KiwoomRestClient() as rest:
            result = await discover_and_arm(
                rest,
                runner,
                top_n=top_n,
                exclude_etf=exclude_etf,
                strategy_factory=strategy_factory,
            )
        console.print(f"[cyan]발굴 결과:[/cyan] {result.note_ko}")
        await runner.notifier.notify(
            "daytrade.discovery_done",
            chosen=[c.symbol for c in result.chosen],
            blocked=result.blocked,
            note=result.note_ko,
        )
        # universe 0 건이면 set_universe() 호출되지 않아 _universe_ready 도 안 set 된다.
        # 청산 시계 task 는 ws.start 이후에 시작되므로 universe 대기 중엔 안 돔 →
        # 러너가 무한 hang. 발굴 실패와 동일하게 명시적으로 stop 트리거.
        # (5/3 21:42 dry-run 에서 게이트 차단으로 chosen=[] → 6시간 hang 실증.)
        if not result.chosen:
            log.info("daytrade.no_candidates_stopping")
            runner.request_stop()
    except Exception as exc:
        log.error("daytrade.discover_failed", error=str(exc), exc_info=exc)
        # 발굴 실패 — universe 안 채워지므로 명시적 stop 으로 hang 회피.
        runner.request_stop()


__all__ = [
    "PaperDayOverrides",
    "apply_daytrade_profile_overrides",
    "apply_profile_to_runner",
    "build_paper_day_runner",
    "build_profile_factory",
    "delayed_discover",
    "paper_day_main",
    "paper_day_with_commands",
]
