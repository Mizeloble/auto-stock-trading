"""`auto-trade` CLI 엔트리.

명령:
- `quote <symbol>`  — 현재가
- `balance`         — 계좌 잔고
- `backtest`        — 과거 데이터 시뮬레이션
- `paper`           — 모의투자 실시간 매매 루프
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import date, datetime, time
from pathlib import Path

import structlog
import typer
from rich.console import Console
from rich.table import Table

from ._common import KST
from .account.balance import get_balance
from .backtest.engine import Backtester, save_result
from .config import get_settings
from .daytrade.orchestrator import discover_and_arm
from .kiwoom.rest import KiwoomRestClient
from .market.chart import bars_to_dataframe, get_daily_bars
from .market.quote import get_quote
from .notify.factory import build_command_handler, build_notifier
from .notify.telegram_commands import TelegramCommandHandler
from .runner import PaperRunner
from .strategy import registry, scanner, suggest
from .strategy.profile import StrategyProfile

app = typer.Typer(help="키움 자동매매 봇 CLI (모의투자 기본)")
strategy_app = typer.Typer(help="전략 추천·프로파일 관리")
app.add_typer(strategy_app, name="strategy")
console = Console()


def _fetch_daily_bars(
    symbol: str,
    *,
    base_dt: date | None = None,
    max_pages: int = 10,
) -> list:
    """일봉 fetch + 빈 결과 가드.

    backtest/advise/suggest 셋이 같은 `KiwoomRestClient → get_daily_bars` 패턴을
    공유. 빈 결과면 사용자에게 안내 메시지 후 `typer.Exit(1)`. df 변환·기간 필터는
    호출자 책임 — 명령마다 lookback·`--from` 처리 방식이 다름.
    """

    async def _run() -> list:
        async with KiwoomRestClient() as client:
            return await get_daily_bars(client, symbol, base_dt=base_dt, max_pages=max_pages)

    bars = asyncio.run(_run())
    if not bars:
        console.print("[red]일봉 데이터를 가져오지 못했습니다. 종목코드/기간을 확인하세요.[/red]")
        raise typer.Exit(code=1)
    return bars


def _setup_logging() -> None:
    level = getattr(logging, get_settings().log_level.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            # 예외 로그에 스택트레이스 포함 — 장애 발생 시 원인 추적용
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
    )


@app.callback()
def _root() -> None:
    _setup_logging()
    s = get_settings()
    mode_color = "green" if s.is_mock else "red bold"
    console.print(f"[dim]키움 모드:[/dim] [{mode_color}]{s.kiwoom_mode.value}[/{mode_color}]")


@app.command()
def quote(symbol: str = typer.Argument(..., help="종목코드 예: 005930")) -> None:
    """현재가 조회."""

    async def _run() -> None:
        async with KiwoomRestClient() as client:
            q = await get_quote(client, symbol)
        table = Table(title=f"현재가 · {symbol}")
        table.add_column("항목")
        table.add_column("값", justify="right")
        table.add_row("현재가", f"{q.price:,}")
        table.add_row("전일 대비", f"{q.change:+,}")
        table.add_row("등락률", f"{q.change_pct:+}%")
        table.add_row("거래량", f"{q.volume:,}")
        console.print(table)

    asyncio.run(_run())


@app.command()
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
        console.print(summary)

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
            console.print(holdings)

    asyncio.run(_run())


@app.command()
def backtest(
    strategy: str | None = typer.Option(
        None, "--strategy", "-s", help="전략 이름 (예: sma_crossover). --profile 과 둘 중 하나."
    ),
    symbol: str | None = typer.Option(None, "--symbol", help="종목코드. --profile 사용 시 생략."),
    start: str = typer.Option(..., "--from", help="YYYY-MM-DD"),
    end: str | None = typer.Option(None, "--to", help="YYYY-MM-DD, 생략 시 오늘"),
    cash: float = typer.Option(10_000_000.0, "--cash", help="초기 예수금"),
    out_dir: Path | None = typer.Option(None, "--out", help="결과 저장 폴더"),
    profile: Path | None = typer.Option(
        None, "--profile", help="전략 프로파일 YAML 경로. --strategy/--symbol 대체."
    ),
) -> None:
    """과거 일봉으로 전략 시뮬레이션."""
    if profile is not None and (strategy or symbol):
        raise typer.BadParameter("--profile 과 --strategy/--symbol 은 동시에 줄 수 없습니다.")
    if profile is None and not (strategy and symbol):
        raise typer.BadParameter("--profile 또는 (--strategy + --symbol) 둘 중 하나가 필요합니다.")

    strategy_name: str
    if profile is not None:
        prof = StrategyProfile.load_yaml(profile)
        strategy_name = prof.strategy
        symbol = prof.symbol
        strat = registry.get(prof.strategy, **prof.params)
        console.print(
            f"[cyan]프로파일 로드:[/cyan] {profile.name} "
            f"(strategy={prof.strategy}, params={prof.params})"
        )
    else:
        assert strategy is not None and symbol is not None  # 위 검증 통과 후 보장
        strategy_name = strategy
        strat = registry.get(strategy)

    base = date.fromisoformat(end) if end else date.today()
    start_date = date.fromisoformat(start)
    bars = _fetch_daily_bars(symbol, base_dt=base, max_pages=10)
    bars = [b for b in bars if b.ts.date() >= start_date]
    if not bars:
        console.print(f"[red]요청한 기간({start}~)에 해당하는 봉이 없습니다.[/red]")
        raise typer.Exit(code=1)

    df = bars_to_dataframe(bars)
    console.print(f"[dim]Bars:[/dim] {len(df)} 개 ({df.index[0].date()} → {df.index[-1].date()})")

    bt = Backtester(strategy=strat, initial_cash=cash)
    result = bt.run(symbol, df)

    rep = result.report.as_dict()
    table = Table(title=f"백테스트 결과 · {strategy_name} · {symbol}")
    table.add_column("지표")
    table.add_column("값", justify="right")
    for k, v in rep.items():
        table.add_row(k, str(v))
    console.print(table)

    out = (
        out_dir
        or Path("data/backtests") / f"{strategy_name}_{symbol}_{datetime.now():%Y%m%d_%H%M%S}"
    )
    saved = save_result(result, out)
    console.print(f"[green]결과 저장:[/green] {saved}")


def _coerce_param(value: str) -> object:
    """'3' → 3, '1.5' → 1.5, '005930' → '005930' (숫자면 숫자, 아니면 문자열)."""
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _parse_symbol_spec(spec: str) -> tuple[str, dict[str, object]]:
    """`005930:short=3:long=15` → ('005930', {'short': 3, 'long': 15}).

    콜론 없으면 심볼만.
    """
    head, *rest = spec.split(":")
    params: dict[str, object] = {}
    for token in rest:
        if "=" not in token:
            raise typer.BadParameter(f"'key=value' 형식이어야 합니다: {token!r}")
        k, v = token.split("=", 1)
        params[k.strip()] = _coerce_param(v.strip())
    return head.strip(), params


@app.command()
def paper(
    strategy: str | None = typer.Option(
        None, "--strategy", "-s", help="전략 이름. --profile 과 둘 중 하나."
    ),
    symbols: str | None = typer.Option(
        None,
        "--symbols",
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
) -> None:
    """실시간 틱을 받아 전략 실행. 모의투자 기본."""
    if profile is not None and (strategy or symbols):
        raise typer.BadParameter("--profile 과 --strategy/--symbols 은 동시에 줄 수 없습니다.")
    if profile is None and not (strategy and symbols):
        raise typer.BadParameter("--profile 또는 (--strategy + --symbols) 둘 중 하나가 필요합니다.")

    strategies: dict[str, object] = {}
    symbol_list: list[str] = []
    if profile is not None:
        prof = StrategyProfile.load_yaml(profile)
        strategies[prof.symbol] = registry.get(prof.strategy, **prof.params)
        symbol_list.append(prof.symbol)
        console.print(
            f"[cyan]프로파일 로드:[/cyan] {profile.name} "
            f"(strategy={prof.strategy}, symbol={prof.symbol})"
        )
    else:
        assert strategy is not None and symbols is not None
        raw_specs = [s.strip() for s in symbols.split(",") if s.strip()]
        if not raw_specs:
            raise typer.BadParameter("symbols 가 비었습니다")
        for spec in raw_specs:
            sym, params = _parse_symbol_spec(spec)
            strategies[sym] = registry.get(strategy, **params)
            symbol_list.append(sym)

    runner = PaperRunner(
        strategies=strategies,  # type: ignore[arg-type]
        symbols=symbol_list,
        bar_minutes=minutes,
        allow_real=allow_real,
        dry_run=dry_run,
        notifier=build_notifier(),
    )
    handler = build_command_handler(
        runner.state_snapshot,
        on_pause=runner.pause,
        on_resume=runner.resume,
        on_stop=runner.request_stop,
    )
    try:
        if handler is None:
            asyncio.run(runner.run())
        else:
            console.print("[cyan]텔레그램 인커밍 명령 폴링 활성[/cyan]")
            asyncio.run(_paper_with_commands(runner, handler))
    except KeyboardInterrupt:
        console.print("[yellow]사용자 중단.[/yellow]")


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


def _parse_hhmm_kst(label: str) -> time:
    """'15:15' 같은 문자열을 KST time 으로. 잘못된 형식이면 BadParameter."""
    try:
        h, m = label.split(":")
        return time(int(h), int(m), tzinfo=KST)
    except (ValueError, AttributeError) as exc:
        raise typer.BadParameter(
            f"--liquidate 는 'HH:MM' (예: 15:15) 형식이어야 합니다: {label!r}"
        ) from exc


@app.command("paper-day")
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
    settings = get_settings()
    color = "green" if settings.is_mock else "red"
    console.print(f"[dim]키움 모드:[/dim] [{color}]{settings.kiwoom_mode.value}[/{color}]")

    liquidate_at = _parse_hhmm_kst(liquidate)
    runner = PaperRunner(
        liquidate_at=liquidate_at,
        bar_minutes=minutes,
        allow_real=allow_real,
        dry_run=dry_run,
        notifier=build_notifier(),
    )
    console.print(
        f"[cyan]day-trade 모드[/cyan] · 청산 {liquidate}"
        f" · top_n={top_n} · {'dry-run' if dry_run else '실주문'}"
    )

    handler = build_command_handler(
        runner.state_snapshot,
        on_pause=runner.pause,
        on_resume=runner.resume,
        on_stop=runner.request_stop,
    )

    exclude_etf = not include_etf
    try:
        if handler is None:
            asyncio.run(
                _paper_day_main(
                    runner,
                    top_n=top_n,
                    discover_delay=discover_delay_sec,
                    exclude_etf=exclude_etf,
                )
            )
        else:
            console.print("[cyan]텔레그램 인커밍 명령 폴링 활성[/cyan]")
            asyncio.run(
                _paper_day_with_commands(
                    runner,
                    handler,
                    top_n=top_n,
                    discover_delay=discover_delay_sec,
                    exclude_etf=exclude_etf,
                )
            )
    except KeyboardInterrupt:
        console.print("[yellow]사용자 중단.[/yellow]")


async def _paper_day_main(
    runner: PaperRunner, *, top_n: int, discover_delay: int, exclude_etf: bool
) -> None:
    """러너를 백그라운드로 시동 + discover_and_arm 으로 universe 주입."""
    runner_task = asyncio.create_task(runner.run(), name="paper-day-runner")
    discover_task = asyncio.create_task(
        _delayed_discover(runner, top_n=top_n, delay=discover_delay, exclude_etf=exclude_etf),
        name="day-discover",
    )
    try:
        await runner_task  # 청산 시각 도달 후 자기 종료
    finally:
        discover_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await discover_task


async def _paper_day_with_commands(
    runner: PaperRunner,
    handler: TelegramCommandHandler,
    *,
    top_n: int,
    discover_delay: int,
    exclude_etf: bool,
) -> None:
    """텔레그램 명령 동시 폴링 버전."""
    runner_task = asyncio.create_task(runner.run(), name="paper-day-runner")
    cmd_task = asyncio.create_task(handler.run(), name="tg-commands")
    discover_task = asyncio.create_task(
        _delayed_discover(runner, top_n=top_n, delay=discover_delay, exclude_etf=exclude_etf),
        name="day-discover",
    )
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
        discover_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await discover_task
        await handler.aclose()


async def _delayed_discover(
    runner: PaperRunner, *, top_n: int, delay: int, exclude_etf: bool = True
) -> None:
    """러너 시동 후 `delay` 초 대기 → discover_and_arm.

    실패해도 러너 자체는 청산 시계로 종료될 수 있게 예외를 swallow + 로그.
    """
    log = structlog.get_logger("daytrade.cli")
    if delay > 0:
        await asyncio.sleep(delay)
    try:
        async with KiwoomRestClient() as rest:
            result = await discover_and_arm(rest, runner, top_n=top_n, exclude_etf=exclude_etf)
        console.print(f"[cyan]발굴 결과:[/cyan] {result.note_ko}")
        await runner.notifier.notify(
            "daytrade.discovery_done",
            chosen=[c.symbol for c in result.chosen],
            blocked=result.blocked,
            note=result.note_ko,
        )
    except Exception as exc:
        log.error("daytrade.discover_failed", error=str(exc), exc_info=exc)
        # 발굴 실패면 universe 가 채워지지 않음 — 러너의 universe_wait 가 청산 시각까지
        # 대기하다가 _liquidation_loop 가 graceful stop 시킨다 (보유 0 이라 noop).
        runner.request_stop()


def _build_anthropic_client() -> object:
    """Settings 의 anthropic_api_key 로 Anthropic 클라이언트 생성. 키 없으면 Exit(1)."""
    s = get_settings()
    if not s.has_anthropic:
        console.print(
            "[red]ANTHROPIC_API_KEY 가 설정되지 않았습니다. "
            ".env 의 ANTHROPIC_API_KEY 를 채우세요.[/red]"
        )
        raise typer.Exit(code=1)
    from anthropic import Anthropic

    return Anthropic(api_key=s.anthropic_api_key.get_secret_value())


@strategy_app.command("advise")
def strategy_advise(
    profile: Path = typer.Option(..., "--profile", help="갱신할 프로파일 YAML 경로"),
    dry_run: bool = typer.Option(False, "--dry-run", help="변경안만 미리보기, 파일은 수정 안 함"),
    lookback: int = typer.Option(
        scanner.DEFAULT_LOOKBACK,
        "--lookback",
        help="시장국면 분석에 쓸 최근 봉 수",
    ),
) -> None:
    """Claude API 로 프로파일의 전략·파라미터·비중을 재조정 (사람이 명시 트리거)."""
    from datetime import datetime as _dt

    from ._common import KST
    from .strategy import llm_advisor as advisor

    client = _build_anthropic_client()
    prof = StrategyProfile.load_yaml(profile)

    bars = _fetch_daily_bars(prof.symbol, max_pages=2)
    df = bars_to_dataframe(bars)
    report = scanner.classify(df, lookback=lookback)
    response = advisor.advise(
        prof,
        df,
        report,
        client=client,  # type: ignore[arg-type]
        model=get_settings().anthropic_model,
    )

    table = Table(title=f"Advisor 제안 · {prof.symbol}")
    table.add_column("항목")
    table.add_column("기존")
    table.add_column("제안")
    table.add_row("strategy", prof.strategy, response.change.strategy)
    table.add_row("params", str(prof.params), str(response.change.params))
    table.add_row("weight", f"{prof.weight}", f"{response.change.weight}")
    console.print(table)
    console.print(f"[dim]근거:[/dim] {response.reason_ko}")
    console.print(f"[dim]confidence:[/dim] {response.confidence:.2f}")

    no_change = (
        prof.strategy == response.change.strategy
        and prof.params == response.change.params
        and prof.weight == response.change.weight
    )
    if no_change:
        console.print("[yellow]변경 없음 — 파일·백업 모두 그대로 둡니다.[/yellow]")
        return
    if dry_run:
        console.print("[yellow]--dry-run 모드: 파일을 수정하지 않습니다.[/yellow]")
        return

    backup = profile.with_suffix(profile.suffix + ".bak")
    backup.write_bytes(profile.read_bytes())
    updated = prof.model_copy(
        update={
            "strategy": response.change.strategy,
            "params": dict(response.change.params),
            "weight": response.change.weight,
            "notes": f"[advisor] {response.reason_ko}",
            "last_advised_at": _dt.now(tz=KST),
        }
    )
    updated.dump_yaml(profile)
    console.print(f"[green]프로파일 갱신:[/green] {profile} (백업: {backup.name})")


@strategy_app.command("suggest")
def strategy_suggest(
    symbol: str = typer.Option(..., "--symbol", help="종목코드 예: 005930"),
    out: Path = typer.Option(
        Path("strategies/profiles"),
        "--out",
        help="프로파일 YAML 저장 폴더 (자동 생성). 파일명은 auto_<symbol>.yaml.",
    ),
    lookback: int = typer.Option(
        scanner.DEFAULT_LOOKBACK,
        "--lookback",
        help="시장국면 분석에 쓸 최근 봉 수 (기본 60).",
    ),
) -> None:
    """최근 차트 패턴을 분석해 어울리는 전략을 추천하고 YAML 프로파일로 저장."""

    bars = _fetch_daily_bars(symbol, max_pages=2)
    df = bars_to_dataframe(bars)
    report = scanner.classify(df, lookback=lookback)
    profile = suggest.recommend(report, symbol)

    table = Table(title=f"시장국면 · {symbol}")
    table.add_column("항목")
    table.add_column("값", justify="right")
    table.add_row("국면", report.regime.value)
    for k, v in report.metrics.items():
        table.add_row(k, f"{v:,.4f}" if isinstance(v, float) else str(v))
    console.print(table)
    console.print(f"[dim]요약:[/dim] {report.summary_ko}")
    console.print(
        f"[green]추천 전략:[/green] {profile.strategy} "
        f"params={profile.params} weight={profile.weight}"
    )

    out_path = out / f"auto_{symbol}.yaml"
    profile.dump_yaml(out_path)
    console.print(f"[green]프로파일 저장:[/green] {out_path}")
    console.print(
        f"[dim]다음 단계:[/dim] uv run auto-trade backtest --profile {out_path} --from 2025-01-01"
    )


if __name__ == "__main__":
    app()
