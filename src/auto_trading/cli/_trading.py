"""실거래·시뮬레이션 명령: quote / balance / verify-symbol / paper / paper-day."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime
from pathlib import Path

import typer
from rich.table import Table

from auto_trading import cli

from .._common import KST
from ..account.balance import get_balance
from ..kiwoom.rest import KiwoomRestClient
from ..notify import insight as insight_mod
from ..notify.telegram_commands import TelegramCommandHandler
from ..runner import PaperRunner
from ..strategy import registry


@cli.app.command()
def quote(symbol: str = typer.Argument(..., help="종목코드 예: 005930")) -> None:
    """현재가 조회."""

    async def _run() -> None:
        async with KiwoomRestClient() as client:
            q = await cli.get_quote(client, symbol)
        table = Table(title=f"현재가 · {symbol}")
        table.add_column("항목")
        table.add_column("값", justify="right")
        table.add_row("현재가", f"{q.price:,}")
        table.add_row("전일 대비", f"{q.change:+,}")
        table.add_row("등락률", f"{q.change_pct:+}%")
        table.add_row("거래량", f"{q.volume:,}")
        cli.console.print(table)

    asyncio.run(_run())


@cli.app.command()
def balance() -> None:
    """계좌 평가/잔고."""

    async def _run() -> None:
        async with KiwoomRestClient() as client:
            snap = await get_balance(client)

        summary = Table(title="계좌 요약")
        summary.add_column("항목")
        summary.add_column("금액", justify="right")
        summary.add_row("예수금", f"{snap.deposit:,}")
        summary.add_row("평가금액", f"{snap.total_evaluation:,}")
        summary.add_row("평가손익", f"{snap.total_pnl:+,}")
        cli.console.print(summary)

        if snap.holdings:
            holdings = Table(title="보유 종목")
            for col in (
                "코드",
                "종목명",
                "수량",
                "평단가",
                "현재가",
                "평가금액",
                "손익",
                "수익률%",
            ):
                holdings.add_column(col, justify="right" if col != "종목명" else "left")
            for h in snap.holdings:
                holdings.add_row(
                    h.symbol,
                    h.name,
                    f"{h.quantity:,}",
                    f"{h.avg_price:,}",
                    f"{h.current_price:,}",
                    f"{h.market_value:,}",
                    f"{h.pnl:+,}",
                    f"{h.pnl_pct:+}%",
                )
            cli.console.print(holdings)

    asyncio.run(_run())


@cli.app.command("verify-symbol")
def verify_symbol(
    symbol: str = typer.Argument(..., help="종목코드 예: 005930"),
    lookback_days: int = typer.Option(
        30, "--days", "-d", help="검사할 최근 일봉 개수 (시간 정순으로 정렬 후 마지막 N개)."
    ),
    gap_threshold: float = typer.Option(
        0.5, "--gap-threshold", help="비정상 갭 임계 (0=비활성, 0.5=±50%). PR29 기본과 동일."
    ),
    fail_on_issues: bool = typer.Option(
        False, "--fail-on-issues", help="issue 1건+이면 exit 1 (cron/CI 가드용)."
    ),
) -> None:
    """키움 일봉 fetch + OHLCV 정합성 검사 — 29단계 PR34 (#19 후속).

    무엇/왜:
    - PR29 의 validate_bars 를 운영에서 직접 호출. 매일 cron 으로 universe 종목의
      데이터 무결성을 자동 점검 가능.
    - 외부 데이터 소스가 깨지면 백테스트가 *거짓 양호* 하게 나옴 → 손실은 실거래에서
      드러남. 사전 자동 점검이 안전망.

    어떻게 확인:
    - `auto-trade verify-symbol 005930 --days 60` → 표 + issue 0건이면 ✓
    - 일부러 broken 데이터로 fetch mock 시 → 각 issue kind/index/detail 표시
    """
    from .._common import Bar
    from ..market.validate import validate_bars

    bars: list[Bar] = cli._fetch_daily_bars(symbol, max_pages=1)
    # 키움 일봉은 최신→과거 순 — 시간 정순으로 뒤집고 lookback 마지막 N개.
    bars = list(reversed(bars))[-lookback_days:]
    if not bars:
        cli.console.print("[red]일봉 데이터가 비어있습니다.[/red]")
        raise typer.Exit(code=1)

    issues = validate_bars(bars, symbol=symbol, gap_threshold=gap_threshold)

    summary = Table(title=f"정합성 검사 · {symbol} · 최근 {len(bars)}일")
    summary.add_column("항목")
    summary.add_column("값", justify="right")
    summary.add_row("검사 bar 수", f"{len(bars)}")
    summary.add_row("발견 이슈", f"{len(issues)}")
    summary.add_row("첫 ts", bars[0].ts.isoformat())
    summary.add_row("마지막 ts", bars[-1].ts.isoformat())
    cli.console.print(summary)

    if not issues:
        cli.console.print("[green]✓ 정합성 검사 통과 — 이슈 0건[/green]")
        return

    detail = Table(title="발견된 이슈")
    detail.add_column("kind")
    detail.add_column("index", justify="right")
    detail.add_column("detail")
    for issue in issues:
        detail.add_row(issue.kind.value, str(issue.index), issue.detail)
    cli.console.print(detail)
    cli.console.print(f"[red]✗ 이슈 {len(issues)}건 — 데이터 소스 점검 필요.[/red]")
    if fail_on_issues:
        raise typer.Exit(code=1)


@cli.app.command()
def paper(
    strategy: str | None = typer.Option(
        None, "--strategy", "-s", help="전략 이름. --profile 과 둘 중 하나."
    ),
    symbols: str | None = typer.Option(
        None,
        "--symbols",
        "--symbol",  # backtest 의 --symbol 과 손가락 호환 (별칭, 이름 변경 아님).
        help=(
            "콤마 구분 종목코드. 종목별 전략 파라미터는 콜론으로 덧붙인다. "
            "예) 005930:short=3:long=15,000660:short=5:long=20. --profile 사용 시 생략."
        ),
    ),
    minutes: int = typer.Option(1, "--minutes", help="집계 봉 길이(분)"),
    allow_real: bool = typer.Option(False, "--allow-real", help="실거래에서도 돌릴지 (위험)"),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="모의투자에서도 실제 주문 전송 없이 시뮬레이션만 (전략·리스크 로직 검증용)",
    ),
    profile: Path | None = typer.Option(
        None, "--profile", help="전략 프로파일 YAML 경로. --strategy/--symbols 대체."
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="KRX 휴장일에도 강제 시동. 수동 dry-run·디버그용. 기본은 휴장일에 안내 후 종료.",
    ),
) -> None:
    """실시간 틱을 받아 전략 실행. 모의투자 기본."""
    cli._holiday_guard(force=force, command_label="paper")
    cli.validate_strategy_source(profile=profile, strategy=strategy, symbol=symbols)

    strategies: dict[str, object] = {}
    symbol_list: list[str] = []
    if profile is not None:
        prof = cli.load_strategy_profile_or_exit(profile)
        strategies[prof.symbol] = registry.get(prof.strategy, **prof.params)
        symbol_list.append(prof.symbol)
        cli.console.print(
            f"[cyan]프로파일 로드:[/cyan] {profile.name} "
            f"(strategy={prof.strategy}, symbol={prof.symbol})"
        )
    else:
        assert strategy is not None and symbols is not None
        raw_specs = [s.strip() for s in symbols.split(",") if s.strip()]
        if not raw_specs:
            raise typer.BadParameter("symbols 가 비었습니다")
        for spec in raw_specs:
            sym, params = cli._parse_symbol_spec(spec)
            strategies[sym] = registry.get(strategy, **params)
            symbol_list.append(sym)

    # 29단계 PR27 — paper-day 와 동일하게 heartbeat writer 명시 주입.
    from ..heartbeat import Heartbeat

    runner = PaperRunner(
        strategies=strategies,  # type: ignore[arg-type]
        symbols=symbol_list,
        bar_minutes=minutes,
        allow_real=allow_real,
        dry_run=dry_run,
        notifier=cli.build_notifier(),
        heartbeat=Heartbeat(),
    )
    handler = cli.build_command_handler(
        runner.state_snapshot,
        on_pause=runner.pause,
        on_resume=runner.resume,
        on_stop=runner.request_stop,
        on_unpause=runner.unpause_loss_tracker,
        on_balance_refresh=runner.force_balance_refresh,
    )
    try:
        if handler is None:
            asyncio.run(runner.run())
        else:
            cli.console.print("[cyan]텔레그램 인커밍 명령 폴링 활성[/cyan]")
            asyncio.run(_paper_with_commands(runner, handler))
    except KeyboardInterrupt:
        cli.console.print("[yellow]사용자 중단.[/yellow]")


async def _paper_with_commands(runner: PaperRunner, handler: TelegramCommandHandler) -> None:
    """러너와 텔레그램 명령 핸들러를 동시에 굴린다.

    하나가 끝나면(보통 러너가 종료되면) 다른 task 도 cancel + aclose.
    """
    runner_task = asyncio.create_task(runner.run(), name="paper-runner")
    cmd_task = asyncio.create_task(handler.run(), name="tg-commands")
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
        # 러너가 raise 했으면 그대로 surface (KeyboardInterrupt 포함)
        for t in done:
            t.result()
    finally:
        await handler.aclose()


@cli.app.command("paper-day")
def paper_day(
    top_n: int = typer.Option(3, "--top-n", help="거래량 상위 후보 종목 수"),
    liquidate: str = typer.Option(
        "15:15", "--liquidate", help="청산 시각 (KST, HH:MM). 정규장 마감 15:30 이전 권장."
    ),
    minutes: int = typer.Option(1, "--minutes", help="집계 봉 길이(분)"),
    allow_real: bool = typer.Option(False, "--allow-real", help="실거래에서도 돌릴지 (위험)"),
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--no-dry-run",
        help="기본 True. 실주문 전환은 며칠 dry-run 검증 후에만.",
    ),
    discover_delay_sec: int = typer.Option(
        30,
        "--discover-delay",
        help="러너 시동 후 발굴까지 대기(초). 09:00 직후 거래량 데이터 안정화 대기용.",
    ),
    include_etf: bool = typer.Option(
        False,
        "--include-etf",
        help="ETF/ETN/SPAC 도 발굴 대상에 포함. 기본은 제외(일반 주식 day-trade 의도).",
    ),
    profile_path: Path | None = typer.Option(
        None,
        "--profile",
        help=(
            "DaytradeProfile YAML 경로. 지정 시 strategy/top_n/liquidate/risk 한도가 "
            "프로파일 값으로 덮어써진다. CLI 인자는 프로파일 없을 때만 적용."
        ),
    ),
    telegram_throttle_min: int = typer.Option(
        0,
        "--telegram-throttle",
        help=(
            "텔레그램 매매 알림 요약 윈도우(분). 0 이면 비활성. 다회 진입·stop·partial 이"
            " 잦은 운용에서 채터링 방지. 5 권장."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="KRX 휴장일에도 강제 시동. 수동 dry-run·디버그용.",
    ),
) -> None:
    """day-trade 모드: 매일 발굴 → 즉시 균등 매수 → 청산 시각에 일괄 매도 → 종료.

    무엇/왜:
    - 21단계. 종목을 미리 정해 들고 가는 swing 모드(`paper`) 와 분리.
    - 발굴: KOSPI+KOSDAQ 거래량 상위 N (market.rank.top_volume).
    - 게이트: 종목 소속 시장 5/20일선 위면 매수 OK (market.index.index_gate).
    - 매수: IntradayMomentum 이 첫 봉에 weight=1/N 시그널 1회. 리스크 매니저가
      종목당 20% / 합계 80% 캡 자동 적용.
    - 청산: 러너의 _liquidation_loop 가 KST 시계 폴링 → 도달 시 보유 전부 시장가
      SELL → request_stop. 시세 끊겨도 자기 종료(4/30 7시간 hang 회귀 방지).

    어떻게 확인:
    - mock 환경에서 `uv run auto-trade paper-day --dry-run` → 로그에
      `daytrade.universe_chosen` → `runner.order_simulated` → 청산 시각에
      `runner.liquidate_simulated` → `runner.stopped` 순서로 찍히면 성공.
    """
    cli._holiday_guard(force=force, command_label="paper-day")
    settings = cli.get_settings()
    color = "green" if settings.is_mock else "red"
    cli.console.print(f"[dim]키움 모드:[/dim] [{color}]{settings.kiwoom_mode.value}[/{color}]")

    # 프로파일 오타·범위 오류를 매매 시동 전에 한국어로 안내(트레이스백 차단).
    # apply_daytrade_profile_overrides 는 cli_helpers(typer-free) 라 여기서 선검증.
    # 29단계 PR41 — 시장국면 자동 라우팅. regime_router.enabled=True 이면
    # KODEX 200 60일봉 → MarketRegime 분류 → trend/defensive/range/crash 매핑 프로파일로
    # 교체. fetch/매핑 실패 시 fail-safe (현재 프로파일 유지). enabled=False(기본) 면 회귀 0.
    if profile_path is not None:
        _pre_profile = cli.load_daytrade_profile_or_exit(profile_path)
        if _pre_profile.regime_router.enabled:
            routing = cli.route_profile_by_regime(
                profile_path,
                fetch_daily_bars=cli._fetch_daily_bars,
            )
            cli.console.print(f"[cyan]{routing.summary_ko}[/cyan]")
            if routing.routed:
                profile_path = routing.profile_path  # 매핑된 새 경로로 교체

    # 23단계 Phase G — 프로파일 있으면 strategy/top_n/liquidate/discover_delay/exclude_etf 덮어쓰기.
    cfg = cli.apply_daytrade_profile_overrides(
        profile_path,
        top_n=top_n,
        liquidate=liquidate,
        discover_delay_sec=discover_delay_sec,
        include_etf=include_etf,
    )
    if cfg.summary_msg:
        cli.console.print(f"[cyan]{cfg.summary_msg}[/cyan]")
    if telegram_throttle_min > 0:
        cli.console.print(f"[cyan]텔레그램 throttle:[/cyan] {telegram_throttle_min}분 윈도우")

    liquidate_at = cli._parse_hhmm_kst(cfg.liquidate)
    # 29단계 PR27 — heartbeat writer 명시 주입. 5/12 회귀(cron 거짓 알람 9건)는
    # build_paper_day_runner 가 Heartbeat 없이 PaperRunner 를 만들어 _heartbeat_loop
    # 자체가 시동되지 않은 게 원인. heartbeat-check cron 의 default path 와 일치.
    from ..heartbeat import Heartbeat

    runner, momentum_tracker = cli.build_paper_day_runner(
        cfg,
        liquidate_at=liquidate_at,
        bar_minutes=minutes,
        allow_real=allow_real,
        dry_run=dry_run,
        telegram_throttle_min=telegram_throttle_min,
        today_kst=datetime.now(KST).date(),
        heartbeat=Heartbeat(),
    )
    cli.console.print(
        f"[cyan]day-trade 모드[/cyan] · 청산 {cfg.liquidate}"
        f" · top_n={cfg.top_n} · {'dry-run' if dry_run else '실주문'}"
    )

    on_insight = None
    if settings.has_anthropic:
        on_insight = insight_mod.build_insight_callback(
            cli._build_anthropic_client(),  # type: ignore[arg-type]
            model=settings.anthropic_model,
        )
    handler = cli.build_command_handler(
        runner.state_snapshot,
        on_pause=runner.pause,
        on_resume=runner.resume,
        on_stop=runner.request_stop,
        on_unpause=runner.unpause_loss_tracker,
        on_insight=on_insight,
        on_balance_refresh=runner.force_balance_refresh,
    )

    exclude_etf = not cfg.include_etf
    try:
        if handler is None:
            asyncio.run(
                cli.paper_day_main(
                    runner,
                    top_n=cfg.top_n,
                    discover_delay=cfg.discover_delay_sec,
                    exclude_etf=exclude_etf,
                    profile=cfg.profile,
                    momentum_tracker=momentum_tracker,
                )
            )
        else:
            cli.console.print("[cyan]텔레그램 인커밍 명령 폴링 활성[/cyan]")
            asyncio.run(
                cli.paper_day_with_commands(
                    runner,
                    handler,
                    top_n=cfg.top_n,
                    discover_delay=cfg.discover_delay_sec,
                    exclude_etf=exclude_etf,
                    profile=cfg.profile,
                    momentum_tracker=momentum_tracker,
                )
            )
    except KeyboardInterrupt:
        cli.console.print("[yellow]사용자 중단.[/yellow]")
