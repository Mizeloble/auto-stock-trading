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
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path
from typing import Any

import structlog
from rich.console import Console

from ..account.order_journal import DEFAULT_JOURNAL_PATH, OrderJournal
from ..heartbeat import Heartbeat
from ..kiwoom.rest import KiwoomRestClient
from ..market.chart import bars_to_dataframe
from ..market.index import IndexMomentumTracker, Market
from ..notify.factory import build_notifier
from ..notify.reporting import ReportingNotifier
from ..notify.telegram_commands import TelegramCommandHandler
from ..notify.throttle import ThrottledNotifier
from ..runner import PaperRunner
from ..runner.day_state import DayStateStore
from ..strategy import scanner
from ..strategy.base import Strategy
from ..strategy.index_daytrade import IndexMomentumDayTrade
from ..strategy.intraday import IntradayMomentum
from ..strategy.regime_router import select_profile_for_regime
from .orchestrator import discover_and_arm, start_momentum_loop
from .profile import DaytradeBreadthGate, DaytradeProfile

console = Console()

# 데이터 기반 전략 수립용 — 그날 시장 국면(regime)을 일자별 로그에 1줄 박제한다.
# reports/daily.py 가 이를 DailyReport.regime 으로 끌어올려 "어떤 국면에서 성과가
# 좋/나빴나" 를 사후 분석할 수 있게 한다. paper-day 시동 시 regime_router 활성일 때만.
_regime_log = structlog.get_logger("daytrade.regime")


# 사용자 노출용 regime → (정의, 무엇을 조정했나, 👍 장점, 👎 단점)
# `feedback_user_facing_tone.md` — YAML 키 금지, 변경마다 정의+👍+👎+이유.
_REGIME_DESCRIPTIONS: dict[scanner.MarketRegime, tuple[str, str, str, str]] = {
    scanner.MarketRegime.TREND_UP: (
        "상승 추세",
        "top_n↑·지수 모멘텀 게이트 살짝 완화",
        "추세장 진입 기회 확대",
        "박스권 전환 직후엔 헛매수 가능",
    ),
    scanner.MarketRegime.TREND_DOWN: (
        "하락 추세",
        "top_n↓·일 노출 절반·손절 좁게",
        "약세장 손실 통제·과매매 차단",
        "반등 초입 기회 일부 놓침",
    ),
    scanner.MarketRegime.RANGE: (
        "박스권",
        "짧은 부분익절·짧은 트레일링",
        "박스 상단 빠른 차익 회수",
        "추세 전환 초입 놓칠 수 있음",
    ),
    scanner.MarketRegime.HIGH_VOL: (
        "고변동성",
        "소액 능동매매(포지션·일매수한도 축소·손절 좁게)·종목 3개 분산",
        "좋은 날은 작게라도 진입·한 종목 쏠림 완화",
        "변동성 손실 위험 — 사이즈를 줄여 흡수(완전정지는 crash 수동 전환)",
    ),
}


@dataclass(frozen=True)
class RegimeRoutingResult:
    """``route_profile_by_regime`` 결과 — 선택된 경로 + 사용자 노출 메시지."""

    profile_path: Path  # 최종 선택된 프로파일 경로 (fail-safe 시 입력 current 그대로)
    regime: scanner.MarketRegime | None  # 분류 실패 시 None
    routed: bool  # True = 매핑 적용됐음, False = fallback/실패
    summary_ko: str  # 사용자 노출용 1~2줄. console 에 그대로 출력 가능.


def route_profile_by_regime(
    current_path: Path,
    *,
    fetch_daily_bars: Callable[[str], Iterable[Any]],
    profiles_root: Path | None = None,
    kodex_symbol: str = "069500",
    lookback: int = 60,
) -> RegimeRoutingResult:
    """KODEX 200 60일봉 → ``scanner.classify`` → ``select_profile_for_regime`` → 경로 결정.

    무엇/왜:
    - ``DaytradeRegimeRouter.enabled=True`` 인 프로파일이 paper-day 시동 시 호출.
    - 매핑된 프로파일이 존재하면 그 경로, 없으면(또는 fetch/classify 실패하면)
      ``current_path`` 그대로 반환 (fail-safe — 봇이 멈추지 않게).

    설계:
    - ``fetch_daily_bars`` 콜백 주입 — 테스트에서 fake bars 로 monkeypatch 가능.
      운영 호출은 ``cli._fetch_daily_bars`` 를 그대로 넘김. cli_helpers 가 직접
      keepalive 클라이언트를 만들지 않는다(외부 호출은 cli 측 책임).

    어떻게 확인: tests/daytrade/test_regime_router_integration.py.
    """
    root = profiles_root or current_path.parent
    try:
        bars = fetch_daily_bars(kodex_symbol)
        df = bars_to_dataframe(bars).tail(lookback)
        report = scanner.classify(df, lookback=lookback)
    except Exception as exc:
        return RegimeRoutingResult(
            profile_path=current_path,
            regime=None,
            routed=False,
            summary_ko=(f"⚠️ 시장 국면 분류 실패(KODEX 200 일봉 fetch): {exc}. 현재 프로파일 유지."),
        )

    selected = select_profile_for_regime(
        report.regime,
        profiles_root=root,
        fallback=current_path,
    )
    if selected is None or selected == current_path:
        # 매핑된 파일 없음 → fallback 유지. 사용자에게 어떤 파일이 빠졌는지 알림.
        _regime_log.info(
            "daytrade.regime",
            regime=report.regime.value,
            routed=False,
            profile=current_path.name,
        )
        return RegimeRoutingResult(
            profile_path=current_path,
            regime=report.regime,
            routed=False,
            summary_ko=(
                f"📊 시장 국면={report.regime.value} 이지만 매핑 프로파일이 없어 "
                f"현재({current_path.name}) 유지. {report.summary_ko}"
            ),
        )

    label, change, pros, cons = _REGIME_DESCRIPTIONS.get(
        report.regime, (report.regime.value, "조정 없음", "-", "-")
    )
    summary = (
        f"📊 시장 국면 자동 라우팅: {label} → {selected.name} ({change}). 👍 {pros}. 👎 {cons}."
    )
    _regime_log.info(
        "daytrade.regime",
        regime=report.regime.value,
        routed=True,
        profile=selected.name,
    )
    return RegimeRoutingResult(
        profile_path=selected,
        regime=report.regime,
        routed=True,
        summary_ko=summary,
    )


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
    heartbeat: Heartbeat | None = None,
) -> tuple[PaperRunner, IndexMomentumTracker | None]:
    """notifier 체인 + PaperRunner + ReportingNotifier wrap + (옵션) momentum tracker 까지 한 번에.

    반환:
    - runner — `runner.notifier` 가 ReportingNotifier 로 감싸진 상태.
    - momentum_tracker — `index_momentum_daytrade` 프로파일에서만 비-None.

    호출자는 (필요 시) command handler 만 별도 wiring 후 paper_day_main / paper_day_with_commands
    중 하나를 await 하면 된다.

    `heartbeat` 미지정이면 비활성(테스트 호환). cli.py paper-day 는 `Heartbeat()` 를 명시 주입해
    cron `heartbeat-check` 와 같은 default path(`data/heartbeat/runner.txt`) 의 mtime 을 갱신한다.
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
        heartbeat=heartbeat,
        # 2026-06-11 — 주문 저널이 러너에 배선돼 있지 않아 시동 시 복구 스캔
        # (_warn_recovery_pending)·preflight 의 no_pending_orders 게이트가 항상
        # 빈 파일을 보고 있었다. ops/preflight 의 기본 경로와 동일 파일 사용.
        journal=OrderJournal(path=DEFAULT_JOURNAL_PATH),
        # 비정상 재시동 시 일일 한도 기준(baseline·매수대금·BUY 카운트·churn) 복원.
        day_state=DayStateStore(),
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
    # 29단계 PR43 — 신규 안전망 옵션 매핑 (PR9/10 #14 + PR27/28 #15).
    # 기본 비활성(0) 이면 RiskLimits 기본 그대로 → 기존 동작 보존.
    limits.max_consecutive_losses = risk.max_consecutive_losses
    limits.consecutive_loss_pause_sec = risk.consecutive_loss_pause_sec
    limits.max_position_correlation = risk.max_position_correlation
    limits.max_buys_per_symbol = risk.max_buys_per_symbol
    limits.daily_realized_loss_limit = risk.daily_realized_loss_limit
    # 종목당/전체 노출 한도 — None 이면 엔진 기본(0.20/0.80) 유지(기존 동작 보존).
    if risk.max_position_weight is not None:
        limits.max_position_weight = risk.max_position_weight
    if risk.max_portfolio_exposure is not None:
        limits.max_portfolio_exposure = risk.max_portfolio_exposure


def build_profile_factory(
    profile: DaytradeProfile,
    tracker: IndexMomentumTracker,
) -> Callable[..., Strategy]:
    """프로파일 → strategy_factory. discover_and_arm 이 (sym, market=...) 으로 호출."""
    # 신호 weight 와 risk 의 max_position_weight 는 manager.evaluate 에서 곱해진다.
    # max_position_weight 를 명시한 프로파일은 종목당 비중을 그 한도로 직접 통제하려는
    # 의도이므로 신호 weight 를 1.0(중립)으로 둬 이중곱을 없앤다. 미설정 프로파일은
    # 기존 균등분할(1/top_n) 동작을 그대로 보존한다.
    weight = 1.0 if profile.risk.max_position_weight is not None else 1.0 / max(1, profile.top_n)

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
            breadth_gate_config=profile.breadth_gate if profile else None,
            avoid_symbols=tuple(profile.avoid_symbols) if profile else (),
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
            breadth_gate_config=profile.breadth_gate if profile else None,
            avoid_symbols=tuple(profile.avoid_symbols) if profile else (),
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
    breadth_gate_config: DaytradeBreadthGate | None = None,
    avoid_symbols: tuple[str, ...] | list[str] = (),
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
                breadth_gate_config=breadth_gate_config,
                avoid_symbols=avoid_symbols,
            )
        console.print(f"[cyan]발굴 결과:[/cyan] {result.note_ko}")
        # 종목명도 함께 — 텔레그램이 코드+이름 ("005930 삼성전자") 으로 표시.
        # result.names 는 chosen·blocked 를 아우른 전체 후보 매핑이라 게이트 차단으로
        # chosen 이 비어도 차단 종목 이름이 그대로 노출된다.
        await runner.notifier.notify(
            "daytrade.discovery_done",
            chosen=[c.symbol for c in result.chosen],
            blocked=result.blocked,
            names=result.names,
            note=result.note_ko,
        )
        # universe 0 건이면 set_universe() 호출되지 않아 _universe_ready 도 안 set 된다.
        # 청산 시계 task 는 ws.start 이후에 시작되므로 universe 대기 중엔 안 돔 →
        # 러너가 무한 hang. 발굴 실패와 동일하게 명시적으로 stop 트리거.
        # (5/3 21:42 dry-run 에서 게이트 차단으로 chosen=[] → 6시간 hang 실증.)
        if not result.chosen:
            log.info("daytrade.no_candidates_stopping")
            runner.request_stop(reason="no_candidates")
    except Exception as exc:
        log.error("daytrade.discover_failed", error=str(exc), exc_info=exc)
        # 발굴 실패 — universe 안 채워지므로 명시적 stop 으로 hang 회피.
        runner.request_stop(reason="discover_failed")


__all__ = [
    "PaperDayOverrides",
    "RegimeRoutingResult",
    "apply_daytrade_profile_overrides",
    "apply_profile_to_runner",
    "build_paper_day_runner",
    "build_profile_factory",
    "delayed_discover",
    "paper_day_main",
    "paper_day_with_commands",
    "route_profile_by_regime",
]
