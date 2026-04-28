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
from datetime import date, datetime
from pathlib import Path

import structlog
import typer
from rich.console import Console
from rich.table import Table

from .account.balance import get_balance
from .backtest.engine import Backtester, save_result
from .config import get_settings
from .kiwoom.rest import KiwoomRestClient
from .market.chart import bars_to_dataframe, get_daily_bars
from .market.quote import get_quote
from .notify.factory import build_command_handler, build_notifier
from .notify.state import RunnerControl
from .notify.telegram_commands import TelegramCommandHandler
from .runner import PaperRunner
from .strategy import registry, scanner, suggest
from .strategy.profile import StrategyProfile

app = typer.Typer(help="키움 자동매매 봇 CLI (모의투자 기본)")
strategy_app = typer.Typer(help="전략 추천·프로파일 관리")
app.add_typer(strategy_app, name="strategy")
console = Console()


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

    async def _fetch() -> list:
        base = date.fromisoformat(end) if end else date.today()
        async with KiwoomRestClient() as client:
            return await get_daily_bars(client, symbol, base_dt=base, max_pages=10)

    start_date = date.fromisoformat(start)
    bars = asyncio.run(_fetch())
    bars = [b for b in bars if b.ts.date() >= start_date]
    if not bars:
        console.print("[red]데이터를 가져오지 못했습니다. 종목코드/기간을 확인하세요.[/red]")
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
    control = RunnerControl(
        pause=runner.pause,
        resume=runner.resume,
        request_stop=runner.request_stop,
    )
    handler = build_command_handler(runner.state_snapshot, control=control)
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

    async def _fetch() -> list:
        async with KiwoomRestClient() as kc:
            return await get_daily_bars(kc, prof.symbol, max_pages=2)

    bars = asyncio.run(_fetch())
    if not bars:
        console.print("[red]일봉을 가져오지 못했습니다.[/red]")
        raise typer.Exit(code=1)

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

    async def _fetch() -> list:
        async with KiwoomRestClient() as client:
            return await get_daily_bars(client, symbol, max_pages=2)

    bars = asyncio.run(_fetch())
    if not bars:
        console.print("[red]일봉 데이터를 가져오지 못했습니다. 종목코드를 확인하세요.[/red]")
        raise typer.Exit(code=1)

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
