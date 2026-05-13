"""`auto-trade` CLI 엔트리.

명령:
- `quote <symbol>`  — 현재가
- `balance`         — 계좌 잔고
- `backtest`        — 과거 데이터 시뮬레이션
- `paper`           — 모의투자 실시간 매매 루프
- `paper-day`       — day-trade 모드 (발굴→매수→15:15 청산)
- `report`          — 그날의 paper-day 로그를 일일 리포트 JSON 으로 집계 (수동)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import asdict
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
from .daytrade.cli_helpers import (
    apply_daytrade_profile_overrides,
    build_paper_day_runner,
    paper_day_main,
    paper_day_with_commands,
)
from .kiwoom.rest import KiwoomRestClient
from .market import calendar as krx_calendar
from .market.chart import bars_to_dataframe, get_daily_bars
from .market.quote import get_quote
from .news.cli_helpers import (
    WeeklyInputs,
    run_weekly_proposal_cycle,
    should_skip_weekly_proposal_for_holiday,
)
from .notify import insight as insight_mod
from .notify.factory import build_command_handler, build_notifier
from .notify.state import format_daily_report
from .notify.telegram_commands import TelegramCommandHandler
from .reports.daily import build_daily_report, save_daily_report
from .reports.log_parser import parse_log
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
    # httpx 의 INFO 레벨은 풀 URL 을 그대로 찍는다. 텔레그램 sendMessage 의
    # 경로 `/bot{TOKEN}/sendMessage` 가 stdout/log 에 노출되는 회귀 원인
    # (2026-05-09 발견 — paper-day 로그에 토큰 1,500+ 회 누적). 운영 시 항상
    # WARNING 으로 강등 — 디버그 필요 시 LOG_LEVEL=DEBUG 면 그때만 풀어준다.
    httpx_level = level if level <= logging.DEBUG else logging.WARNING
    logging.getLogger("httpx").setLevel(httpx_level)
    logging.getLogger("httpcore").setLevel(httpx_level)


@app.callback()
def _root() -> None:
    _setup_logging()
    s = get_settings()
    mode_color = "green" if s.is_mock else "red bold"
    console.print(f"[dim]키움 모드:[/dim] [{mode_color}]{s.kiwoom_mode.value}[/{mode_color}]")


def _holiday_guard(*, force: bool, command_label: str) -> None:
    """KRX 휴장일이면 텔레그램 1회 안내 후 정상 종료.

    `--force` 면 우회 — 수동 dry-run·디버그용. paper / paper-day / weekly-proposal
    셋이 공통 호출. launchd 가 평일에만 시동하지만 한국 법정공휴일·임시휴장에는
    빈 universe 발굴 → 즉시 종료 → 텔레그램 노이즈가 발생해 진입 자체를 막는다.
    """
    if force:
        return
    today = krx_calendar.today_kst()
    if krx_calendar.is_trading_day(today):
        return

    log = structlog.get_logger("auto_trading.cli")
    log.info("runner.holiday_skip", command=command_label, date=today.isoformat())
    console.print(
        f"[yellow]📅 오늘({today.isoformat()})은 KRX 휴장일 — {command_label} 시동 생략.[/yellow]"
    )
    next_day = krx_calendar.next_trading_day(today)
    notifier = build_notifier()

    async def _push() -> None:
        try:
            await notifier.notify(
                "runner.holiday_skip",
                command=command_label,
                date=today.isoformat(),
                next_trading_day=next_day.isoformat(),
            )
        finally:
            await notifier.aclose()

    asyncio.run(_push())
    raise typer.Exit(code=0)


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


def _parse_walk_forward_spec(spec: str) -> tuple[int, int]:
    """\"90/30\" 또는 \"90d/30d\" → (window_days=90, step_days=30)."""
    parts = spec.replace("d", "").split("/")
    if len(parts) != 2:
        raise typer.BadParameter("--walk-forward 는 '90/30' 또는 '90d/30d' 형식이어야 합니다.")
    try:
        window = int(parts[0])
        step = int(parts[1])
    except ValueError as exc:
        raise typer.BadParameter(f"숫자 파싱 실패: {spec}") from exc
    if window <= 0 or step <= 0:
        raise typer.BadParameter("window/step 둘 다 양수여야 합니다.")
    return window, step


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
    walk_forward_spec: str | None = typer.Option(
        None,
        "--walk-forward",
        help="walk-forward 모드. 'window/step' 일 단위 (예: '90/30' = 90일 윈도우, 30일 간격).",
    ),
) -> None:
    """과거 일봉으로 전략 시뮬레이션.

    \\--walk-forward 활성 시 단일 백테스트 대신 슬라이딩 윈도우로 분할 평가 —
    윈도우별 metric 분포로 전략의 시간 일관성 확인.
    """
    if profile is not None and (strategy or symbol):
        raise typer.BadParameter("--profile 과 --strategy/--symbol 은 동시에 줄 수 없습니다.")
    if profile is None and not (strategy and symbol):
        raise typer.BadParameter("--profile 또는 (--strategy + --symbol) 둘 중 하나가 필요합니다.")

    strategy_name: str
    strategy_params: dict[str, object]
    if profile is not None:
        prof = StrategyProfile.load_yaml(profile)
        strategy_name = prof.strategy
        symbol = prof.symbol
        strategy_params = dict(prof.params)
        strat = registry.get(prof.strategy, **prof.params)
        console.print(
            f"[cyan]프로파일 로드:[/cyan] {profile.name} "
            f"(strategy={prof.strategy}, params={prof.params})"
        )
    else:
        assert strategy is not None and symbol is not None  # 위 검증 통과 후 보장
        strategy_name = strategy
        strategy_params = {}
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

    # 29단계 PR24 — walk-forward 분기
    if walk_forward_spec is not None:
        window_days, step_days = _parse_walk_forward_spec(walk_forward_spec)
        _run_walk_forward(
            strategy_name=strategy_name,
            strategy_params=strategy_params,
            symbol=symbol,
            df=df,
            window_days=window_days,
            step_days=step_days,
            cash=cash,
            out_dir=out_dir,
        )
        return

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


def _run_walk_forward(
    *,
    strategy_name: str,
    strategy_params: dict[str, object],
    symbol: str,
    df,  # pd.DataFrame
    window_days: int,
    step_days: int,
    cash: float,
    out_dir: Path | None,
) -> None:
    """walk-forward 백테스트 실행 + 표 출력 + 결과 저장.

    각 윈도우마다 새 strategy 인스턴스 (state 누설 방지). 저장 형식:
    - windows.csv: 윈도우별 메트릭 행.
    - summary.json: WalkForwardSummary as_dict.
    """
    import csv as _csv
    import json as _json

    from .backtest.walk_forward import walk_forward

    def _factory():
        return registry.get(strategy_name, **strategy_params)

    result = walk_forward(
        _factory,
        symbol,
        df,
        window_days=window_days,
        step_days=step_days,
        initial_cash=cash,
    )

    if result.summary.n_windows == 0:
        console.print(
            f"[yellow]Walk-forward 윈도우 0건 — 데이터 부족 (window_days={window_days}).[/yellow]"
        )
        raise typer.Exit(code=1)

    # 윈도우 표.
    table = Table(
        title=f"Walk-Forward · {strategy_name} · {symbol} "
        f"(window={window_days}d / step={step_days}d)"
    )
    table.add_column("#", justify="right")
    table.add_column("기간")
    table.add_column("Sharpe", justify="right")
    table.add_column("CAGR%", justify="right")
    table.add_column("MDD%", justify="right")
    table.add_column("WinRate%", justify="right")
    table.add_column("거래", justify="right")
    for i, w in enumerate(result.windows, start=1):
        rep = w.report
        s_color = "green" if rep.sharpe > 0 else "red"
        table.add_row(
            str(i),
            f"{w.start.date()} → {w.end.date()}",
            f"[{s_color}]{rep.sharpe:.3f}[/{s_color}]",
            f"{rep.cagr * 100:+.2f}",
            f"{rep.max_drawdown * 100:.2f}",
            f"{rep.win_rate * 100:.1f}",
            str(rep.num_trades),
        )
    console.print(table)

    # Summary.
    s = result.summary
    summary_table = Table(title="Summary")
    summary_table.add_column("지표")
    summary_table.add_column("값", justify="right")
    summary_table.add_row("윈도우 개수", str(s.n_windows))
    summary_table.add_row("Sharpe 평균", f"{s.sharpe_mean:.3f}")
    summary_table.add_row("Sharpe 표준편차", f"{s.sharpe_std:.3f}")
    summary_table.add_row("Sharpe min/max", f"{s.sharpe_min:.3f} / {s.sharpe_max:.3f}")
    summary_table.add_row("양수 Sharpe 비율", f"{s.positive_sharpe_ratio:.1%}")
    summary_table.add_row("CAGR 평균%", f"{s.cagr_mean_pct:+.2f}")
    summary_table.add_row("CAGR 표준편차%", f"{s.cagr_std_pct:.2f}")
    summary_table.add_row("최악 MDD%", f"{s.mdd_worst_pct:.2f}")
    summary_table.add_row("승률 평균%", f"{s.win_rate_mean_pct:.2f}")
    console.print(summary_table)

    # 저장.
    out = (
        out_dir
        or Path("data/backtests") / f"{strategy_name}_{symbol}_wf_{datetime.now():%Y%m%d_%H%M%S}"
    )
    out.mkdir(parents=True, exist_ok=True)

    windows_path = out / "windows.csv"
    with windows_path.open("w", newline="") as f:
        writer = _csv.writer(f)
        writer.writerow(
            ["start", "end", "sharpe", "cagr", "max_drawdown", "win_rate", "num_trades"]
        )
        for w in result.windows:
            r = w.report
            writer.writerow(
                [
                    w.start.isoformat(),
                    w.end.isoformat(),
                    f"{r.sharpe:.6f}",
                    f"{r.cagr:.6f}",
                    f"{r.max_drawdown:.6f}",
                    f"{r.win_rate:.6f}",
                    r.num_trades,
                ]
            )

    summary_path = out / "summary.json"
    summary_path.write_text(
        _json.dumps(
            {
                "n_windows": s.n_windows,
                "sharpe_mean": s.sharpe_mean,
                "sharpe_std": s.sharpe_std,
                "sharpe_min": s.sharpe_min,
                "sharpe_max": s.sharpe_max,
                "cagr_mean_pct": s.cagr_mean_pct,
                "cagr_std_pct": s.cagr_std_pct,
                "mdd_worst_pct": s.mdd_worst_pct,
                "win_rate_mean_pct": s.win_rate_mean_pct,
                "positive_sharpe_ratio": s.positive_sharpe_ratio,
                "window_days": window_days,
                "step_days": step_days,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    console.print(f"[green]결과 저장:[/green] {out}")


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
    force: bool = typer.Option(
        False,
        "--force",
        help="KRX 휴장일에도 강제 시동. 수동 dry-run·디버그용. 기본은 휴장일에 안내 후 종료.",
    ),
) -> None:
    """실시간 틱을 받아 전략 실행. 모의투자 기본."""
    _holiday_guard(force=force, command_label="paper")
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

    # 29단계 PR27 — paper-day 와 동일하게 heartbeat writer 명시 주입.
    from .heartbeat import Heartbeat

    runner = PaperRunner(
        strategies=strategies,  # type: ignore[arg-type]
        symbols=symbol_list,
        bar_minutes=minutes,
        allow_real=allow_real,
        dry_run=dry_run,
        notifier=build_notifier(),
        heartbeat=Heartbeat(),
    )
    handler = build_command_handler(
        runner.state_snapshot,
        on_pause=runner.pause,
        on_resume=runner.resume,
        on_stop=runner.request_stop,
        on_unpause=runner.unpause_loss_tracker,
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
    _holiday_guard(force=force, command_label="paper-day")
    settings = get_settings()
    color = "green" if settings.is_mock else "red"
    console.print(f"[dim]키움 모드:[/dim] [{color}]{settings.kiwoom_mode.value}[/{color}]")

    # 23단계 Phase G — 프로파일 있으면 strategy/top_n/liquidate/discover_delay/exclude_etf 덮어쓰기.
    cfg = apply_daytrade_profile_overrides(
        profile_path,
        top_n=top_n,
        liquidate=liquidate,
        discover_delay_sec=discover_delay_sec,
        include_etf=include_etf,
    )
    if cfg.summary_msg:
        console.print(f"[cyan]{cfg.summary_msg}[/cyan]")
    if telegram_throttle_min > 0:
        console.print(f"[cyan]텔레그램 throttle:[/cyan] {telegram_throttle_min}분 윈도우")

    liquidate_at = _parse_hhmm_kst(cfg.liquidate)
    # 29단계 PR27 — heartbeat writer 명시 주입. 5/12 회귀(cron 거짓 알람 9건)는
    # build_paper_day_runner 가 Heartbeat 없이 PaperRunner 를 만들어 _heartbeat_loop
    # 자체가 시동되지 않은 게 원인. heartbeat-check cron 의 default path 와 일치.
    from .heartbeat import Heartbeat

    runner, momentum_tracker = build_paper_day_runner(
        cfg,
        liquidate_at=liquidate_at,
        bar_minutes=minutes,
        allow_real=allow_real,
        dry_run=dry_run,
        telegram_throttle_min=telegram_throttle_min,
        today_kst=datetime.now(KST).date(),
        heartbeat=Heartbeat(),
    )
    console.print(
        f"[cyan]day-trade 모드[/cyan] · 청산 {cfg.liquidate}"
        f" · top_n={cfg.top_n} · {'dry-run' if dry_run else '실주문'}"
    )

    on_insight = None
    if settings.has_anthropic:
        on_insight = insight_mod.build_insight_callback(
            _build_anthropic_client(),  # type: ignore[arg-type]
            model=settings.anthropic_model,
        )
    handler = build_command_handler(
        runner.state_snapshot,
        on_pause=runner.pause,
        on_resume=runner.resume,
        on_stop=runner.request_stop,
        on_unpause=runner.unpause_loss_tracker,
        on_insight=on_insight,
    )

    exclude_etf = not cfg.include_etf
    try:
        if handler is None:
            asyncio.run(
                paper_day_main(
                    runner,
                    top_n=cfg.top_n,
                    discover_delay=cfg.discover_delay_sec,
                    exclude_etf=exclude_etf,
                    profile=cfg.profile,
                    momentum_tracker=momentum_tracker,
                )
            )
        else:
            console.print("[cyan]텔레그램 인커밍 명령 폴링 활성[/cyan]")
            asyncio.run(
                paper_day_with_commands(
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
        console.print("[yellow]사용자 중단.[/yellow]")


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


@app.command()
def report(
    target_date: str = typer.Option(
        "",
        "--date",
        help="ISO YYYY-MM-DD. 미지정 시 오늘(KST). paper-day 가 청산 직후 자동 푸시한 같은 형식의 리포트를 수동 재생성.",
    ),
    log_path: Path = typer.Option(
        None,
        "--log",
        help="로그 파일 경로. 미지정 시 logs/paper-day-<date>.log 자동 사용.",
    ),
    out: Path = typer.Option(
        Path("data/reports"),
        "--out",
        help="리포트 JSON 저장 폴더. 자동 생성.",
    ),
    push: bool = typer.Option(
        False,
        "--push/--no-push",
        help="True 면 텔레그램에 푸시. 기본은 콘솔만.",
    ),
) -> None:
    """22단계 Phase A — 그날의 paper-day 로그를 파싱해 일일 리포트 생성·저장.

    무엇/왜:
    - 운영 중 자동 푸시(러너 종료 시) 가 실패했거나 과거 일자를 다시 보고 싶을 때.
    - 결정론적 — 같은 로그면 같은 출력. LLM 호출 없음.

    어떻게 확인: 콘솔에 한국어 요약 출력 + `data/reports/daily-YYYY-MM-DD.json` 파일 생성.
    """
    iso = target_date or datetime.now(KST).date().isoformat()
    log_file = log_path or Path(f"logs/paper-day-{iso}.log")
    if not log_file.exists():
        console.print(f"[red]로그 파일 없음:[/red] {log_file}")
        raise typer.Exit(code=1)

    events = parse_log(log_file)
    daily = build_daily_report(date=iso, events=events, snapshot=None)
    save_path = save_daily_report(daily, root=out)
    console.print(f"[green]리포트 저장:[/green] {save_path}")

    # asdict 으로 신규 필드 자동 포함. discovery_blocked tuple 만 list 로 호환 변환.
    payload = asdict(daily)
    payload["discovery_blocked"] = [list(t) for t in daily.discovery_blocked]
    console.print(format_daily_report(payload))

    if push:
        notifier = build_notifier()

        async def _push() -> None:
            try:
                await notifier.notify("runner.daily_report", **payload)
            finally:
                await notifier.aclose()

        asyncio.run(_push())
        console.print("[green]텔레그램 푸시 완료[/green]")


@app.command()
def insight(
    days: int = typer.Option(
        7,
        "--days",
        help="직전 N일치 일일 리포트(JSON) 를 LLM 입력으로 사용. 기본 7.",
    ),
    reports_root: Path = typer.Option(
        Path("data/reports"),
        "--reports-root",
        help="`daily-*.json` 이 쌓인 폴더.",
    ),
    push: bool = typer.Option(
        False,
        "--push/--no-push",
        help="True 면 텔레그램에 푸시. 기본은 콘솔만.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="LLM 호출 없이 입력 요약만 출력 (테스트·디버그용).",
    ),
) -> None:
    """22단계 Phase B — 직전 N일치 일일 리포트를 LLM 으로 누적 분석.

    무엇/왜:
    - 단일 일자 정보는 `auto-trade report` 가 다룸. 여러 날 쌓인 패턴·이상치는
      LLM 만이 잘 보는 영역.
    - LLM 비용 통제: 사람 명시 트리거(또는 사전 합의된 토 cron) 만 허용.
      매일·매시간 자동 호출 X (메모리 `feedback_llm_cost.md`).
    - 입력 토큰 절약: 풀 JSON 대신 요약 dict + system 프롬프트 캐싱.

    어떻게 확인:
    - `--dry-run` 으로 입력 요약 확인 → 본 실행에서 한국어 인사이트 출력 + 옵션 텔레그램.
    """
    reports = insight_mod.collect_recent_reports(root=reports_root, days=days)
    if not reports:
        console.print(
            f"[red]리포트 데이터 부족:[/red] {reports_root}/daily-*.json 을 찾지 못했습니다. "
            "먼저 `auto-trade report --date YYYY-MM-DD` 로 일일 리포트를 생성하세요."
        )
        raise typer.Exit(code=1)

    date_range = (reports[0].date, reports[-1].date)
    console.print(f"[cyan]입력:[/cyan] {len(reports)}일 ({date_range[0]}~{date_range[1]})")

    if dry_run:
        from .notify.insight import _summarize_reports

        console.print(_summarize_reports(reports))
        return

    client = _build_anthropic_client()
    response = insight_mod.insight(
        reports,
        client=client,  # type: ignore[arg-type]
        model=get_settings().anthropic_model,
    )
    msg = insight_mod.format_insight_message(response, days=len(reports), date_range=date_range)
    console.print(msg)

    if push:
        notifier = build_notifier()

        async def _push() -> None:
            try:
                await notifier.notify(
                    "runner.weekly_insight",
                    summary_ko=response.summary_ko,
                    key_findings=list(response.key_findings),
                    recommendations=list(response.recommendations),
                    confidence=response.confidence,
                    days=len(reports),
                    date_from=date_range[0],
                    date_to=date_range[1],
                )
            finally:
                await notifier.aclose()

        asyncio.run(_push())
        console.print("[green]텔레그램 푸시 완료[/green]")


@app.command("weekly-proposal")
def weekly_proposal(
    days_reports: int = typer.Option(7, "--days-reports", help="직전 N일치 일일 리포트 (기본 7)."),
    days_news: int = typer.Option(7, "--days-news", help="직전 N일치 RSS 뉴스 (기본 7)."),
    max_news_items: int = typer.Option(
        80,
        "--max-news-items",
        help="LLM 입력으로 보낼 뉴스 최대 건수 (기본 80). 호출당 input 토큰을 좌우.",
    ),
    force_refresh: bool = typer.Option(
        True,
        "--force-refresh/--no-force-refresh",
        help=(
            "True (기본) 면 같은 날 캐시가 있어도 RSS 를 새로 받아 누적 병합. "
            "토요일 06:00 prefetch cron 이 만든 캐시 + 09:00 사이 새 뉴스를 잃지 않게."
        ),
    ),
    current: Path = typer.Option(
        Path("strategies/profiles/daytrade-current.yaml"),
        "--current",
        help="현재 프로파일 경로 (없으면 fallback 으로 fall back).",
    ),
    fallback: Path = typer.Option(
        Path("strategies/profiles/daytrade-default.yaml"),
        "--fallback",
        help="current 가 없을 때 base 로 사용할 프로파일.",
    ),
    target: Path | None = typer.Option(
        None,
        "--target",
        help="작성할 프로파일 경로. 기본은 current 와 동일 (덮어쓰기 + 백업).",
    ),
    push: bool = typer.Option(False, "--push/--no-push", help="True 면 텔레그램에 변경 요약 푸시."),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="LLM 호출 없이 입력 요약만 출력 (디버그용).",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="KRX 휴장 주에도 LLM 호출. 기본은 토요일이 휴장일과 가까우면 안내 후 종료.",
    ),
) -> None:
    """24단계 Phase B+C+D — 주간 LLM 으로 다음 주 day-trade 프로파일 자동 갱신.

    흐름:
    1. 직전 7일 일일 리포트 + RSS 뉴스 수집
    2. 현재 daytrade-current.yaml 로드 (없으면 daytrade-default.yaml)
    3. propose_daytrade_profile (LLM, 강제 도구) → DaytradeProposal
    4. apply_and_persist (안전 캡 통과한 변경만 적용 + 백업)
    5. format_apply_message → 콘솔 + 옵션 텔레그램

    토요일 09:00 KST cron 또는 사용자 수동 호출용. LLM 호출은 주 1회 한정.
    --force 미지정 시 토요일이 직접 휴장일이거나 다음 거래일까지 5일 이상 떨어져
    있으면(긴 연휴) 시동 생략 — LLM 비용 절약.
    """
    skip_msg = should_skip_weekly_proposal_for_holiday(force=force, today=krx_calendar.today_kst())
    if skip_msg is not None:
        console.print(f"[yellow]📅 {skip_msg}[/yellow]")
        raise typer.Exit(code=0)

    from .news.collector import collect_recent_news
    from .news.proposal import _summarize_news, _summarize_reports

    inputs = WeeklyInputs(
        reports=list(insight_mod.collect_recent_reports(days=days_reports)),
        news=list(collect_recent_news(days=days_news, force_refresh=force_refresh)),
    )
    if inputs.is_empty:
        console.print("[red]데이터 부족:[/red] reports + news 모두 비어있습니다.")
        raise typer.Exit(code=1)

    # 29단계 PR18 (#5 일부) — 현재 시장국면을 LLM 입력에 추가.
    # KOSPI 200 추종 ETF(069500) 60일봉 → classify. fetch 실패는 swallow (regime=None
    # 으로 LLM 호출 — 기존 동작 유지). LLM 비용 절약 위해 dry-run 도 시도.
    regime: scanner.RegimeReport | None = None
    try:
        regime_bars = _fetch_daily_bars("069500")
        regime_df = bars_to_dataframe(regime_bars).tail(60)
        regime = scanner.classify(regime_df, lookback=60)
        console.print(f"[cyan]시장국면:[/cyan] {regime.regime.value} — {regime.summary_ko}")
    except Exception as exc:
        console.print(f"[dim]regime 분류 생략 (fetch 실패): {exc}[/dim]")

    console.print(
        f"[cyan]입력:[/cyan] reports {len(inputs.reports)}건 · news {len(inputs.news)}건 "
        f"(LLM 입력 최대 {max_news_items}건)"
    )

    if dry_run:
        console.print(
            {
                "reports": _summarize_reports(inputs.reports),
                "news": _summarize_news(inputs.news, max_items=max_news_items),
                "regime": regime.regime.value if regime else None,
            }
        )
        return

    _, _, msg = run_weekly_proposal_cycle(
        inputs,
        current_path=current,
        fallback_path=fallback,
        target_path=target,
        client=_build_anthropic_client(),
        model=get_settings().anthropic_model,
        max_news_items=max_news_items,
        regime=regime,
    )
    console.print(msg)

    if push:
        notifier = build_notifier()

        async def _push() -> None:
            try:
                await notifier.notify("runner.weekly_proposal", text=msg)
            finally:
                await notifier.aclose()

        asyncio.run(_push())
        console.print("[green]텔레그램 푸시 완료[/green]")


@app.command("fetch-news")
def fetch_news(
    days: int = typer.Option(7, "--days", help="직전 N일치 RSS 뉴스 (기본 7)."),
    force_refresh: bool = typer.Option(
        True,
        "--force-refresh/--no-force-refresh",
        help="True (기본) 면 캐시가 있어도 RSS 를 새로 받아 누적 병합.",
    ),
) -> None:
    """RSS 뉴스만 미리 fetch → 캐시 갱신 (LLM 호출 없음 · 비용 0).

    무엇/왜:
    - 토요일 06:00 cron 이 호출 → 09:00 `weekly-proposal` 이 사용할 캐시 선반영.
    - 06:00~09:00 사이 새 뉴스는 09:00 호출이 force_refresh=True 로 다시 fetch
      해서 누적 병합. 즉 prefetch 가 실패해도 안전 fallback.

    어떻게 확인: `data/news/news-v3-YYYY-MM-DD.json` 갱신 + 콘솔 건수 표시.
    """
    from .news.collector import collect_recent_news

    news = collect_recent_news(days=days, force_refresh=force_refresh)
    sources: dict[str, int] = {}
    for it in news:
        sources[it.source] = sources.get(it.source, 0) + 1
    console.print(f"[cyan]뉴스 수집:[/cyan] {len(news)}건 (직전 {days}일)")
    for src, cnt in sorted(sources.items(), key=lambda kv: -kv[1]):
        console.print(f"  · {src}: {cnt}건")


@app.command()
def dashboard(
    host: str = typer.Option("127.0.0.1", "--host", help="바인딩 주소. 기본 로컬 전용."),
    port: int = typer.Option(8000, "--port", help="포트."),
    reload: bool = typer.Option(False, "--reload", help="dev 자동 리로드 (기본 경로만 지원)."),
    reports_root: Path = typer.Option(
        Path("data/reports"),
        "--reports-root",
        help="`daily-*.json` 폴더.",
    ),
    backtests_root: Path = typer.Option(
        Path("data/backtests"),
        "--backtests-root",
        help="백테스트 결과 폴더 루트.",
    ),
) -> None:
    """투자 결과 분석 웹 대시보드 (FastAPI · 로컬 전용).

    무엇/왜:
    - `data/reports/daily-*.json`, `data/backtests/<run>/` 를 읽어 누적 자산곡선·일별 상세·
      백테스트 결과를 브라우저에서 시각화. LLM 호출 0건, 외부 네트워크 호출 0건.
    - 기본 host=127.0.0.1 (로컬 전용). 다른 기기에서 접속하려면 `--host 0.0.0.0` 명시.

    어떻게 확인: 명령 실행 후 출력된 URL(`http://127.0.0.1:8000`) 을 브라우저로 열기.
    """
    import uvicorn

    if reload:
        uvicorn.run(
            "auto_trading.dashboard.app:app",
            host=host,
            port=port,
            reload=True,
            reload_dirs=["src/auto_trading/dashboard"],
        )
        return

    from .dashboard.app import create_app

    instance = create_app(reports_root=reports_root, backtests_root=backtests_root)
    console.print(f"[cyan]대시보드:[/cyan] http://{host}:{port}")
    console.print(
        f"[dim]reports={reports_root.resolve()} · backtests={backtests_root.resolve()}[/dim]"
    )
    uvicorn.run(instance, host=host, port=port, log_level="info")


@app.command("heartbeat-check")
def heartbeat_check(
    path: Path = typer.Option(
        Path("data/heartbeat/runner.txt"),
        "--path",
        help="러너가 갱신하는 heartbeat 파일.",
    ),
    max_age_sec: int = typer.Option(
        3600,
        "--max-age-sec",
        help="이 초 이상 갱신 누락이면 'stale' 로 판정.",
    ),
    cooldown_sec: int = typer.Option(
        1800,
        "--cooldown-sec",
        help="같은 누락 상황에서 텔레그램 알림 최소 간격 (spam 방지).",
    ),
    cooldown_path: Path = typer.Option(
        Path("data/heartbeat/last_alert.txt"),
        "--cooldown-path",
        help="cooldown 추적용 빈 파일 경로 (mtime 만 사용).",
    ),
) -> None:
    """봇 heartbeat 누락 검사 — cron 5분 간격 호출 가정.

    무엇/왜:
    - 러너가 background task 로 ``--path`` 를 주기 갱신. 봇이 조용히 멈추면
      mtime 이 멈춤 → 이 명령이 그걸 잡아 텔레그램 경고.
    - 외부 API 의존 0 (디스크 mtime 만). cron 실패와 봇 침묵을 구분 가능.
    - 같은 침묵 상황에서 5분마다 알림 도착하지 않게 ``cooldown_sec`` 1회.

    어떻게 확인: 봇을 SIGSTOP 후 1시간 대기 → 이 명령 실행 → 텔레그램에
    "🚨 heartbeat stale" 도착. 정상 운영 중에는 침묵.
    """
    from .heartbeat import DEFAULT_STOPPED_PATH, check_heartbeat

    # 29단계 회귀(2026-05-11) — 러너가 정상 종료 sentinel 을 남기면 cron 은 침묵.
    # heartbeat 파일과 같은 디렉터리에 있는 stopped.txt 를 본다.
    stopped_path = path.parent / DEFAULT_STOPPED_PATH.name
    status = check_heartbeat(path, max_age_sec, stopped_path=stopped_path)
    if status.ok:
        if status.reason == "stopped":
            console.print(f"[green]heartbeat stopped[/green] · sentinel ok (path={stopped_path})")
        else:
            console.print(f"[green]heartbeat OK[/green] · age={status.age_sec:.0f}s (path={path})")
        raise typer.Exit(code=0)

    # cooldown — 직전 알림이 cooldown_sec 안이면 침묵.
    now_ts = datetime.now().timestamp()
    if cooldown_path.exists():
        since_alert = now_ts - cooldown_path.stat().st_mtime
        if since_alert < cooldown_sec:
            console.print(
                f"[yellow]heartbeat {status.reason}[/yellow] — cooldown 중 "
                f"({since_alert:.0f}s/{cooldown_sec}s, 알림 생략)"
            )
            raise typer.Exit(code=0)

    cooldown_path.parent.mkdir(parents=True, exist_ok=True)
    cooldown_path.touch()

    console.print(f"[red]heartbeat {status.reason}[/red] (path={path})")
    notifier = build_notifier()

    async def _push() -> None:
        try:
            await notifier.notify(
                "runner.heartbeat_alert",
                reason=status.reason,
                age_sec=status.age_sec,
                path=str(path),
                max_age_sec=max_age_sec,
            )
        finally:
            await notifier.aclose()

    asyncio.run(_push())


@app.command()
def preflight(
    profile: Path = typer.Option(
        Path("strategies/profiles/daytrade-current.yaml"),
        "--profile",
        help="검사할 day-trade 프로파일. 없으면 default fallback.",
    ),
    journal: Path = typer.Option(
        Path("data/orders/state.jsonl"),
        "--journal",
        help="주문 저널 경로.",
    ),
    heartbeat_path: Path = typer.Option(
        Path("data/heartbeat/runner.txt"),
        "--heartbeat",
        help="러너 heartbeat 파일.",
    ),
    reports_root: Path = typer.Option(
        Path("data/reports"),
        "--reports-root",
        help="paper-day 일일 리포트 폴더.",
    ),
    backtests_root: Path = typer.Option(
        Path("data/backtests"),
        "--backtests-root",
        help="백테스트 결과 폴더 루트.",
    ),
) -> None:
    """실거래 전환 전 자동 안전 체크리스트.

    무엇/왜:
    - 사용자가 "이제 실거래 켜도 되겠지?" 라고 의식하기 전에 기계가 안전망
      게이트를 일관되게 검사. 사람이 잊을 위험을 줄인다.
    - 모든 활성 게이트가 통과하면 ``--allow-real`` 안내. 하나라도 실패면 exit 1.

    어떻게 확인: 실거래 전 매번 실행. 실패 항목의 detail 에 *다음 액션* 표시.
    """
    if not profile.exists():
        fallback = Path("strategies/profiles/daytrade-default.yaml")
        if fallback.exists():
            console.print(f"[dim]{profile} 없음 → fallback {fallback}[/dim]")
            profile = fallback

    from .preflight import run_all_gates

    settings = get_settings()
    results = run_all_gates(
        profile_path=profile,
        journal_path=journal,
        heartbeat_path=heartbeat_path,
        reports_root=reports_root,
        backtests_root=backtests_root,
        llm_cost_path=settings.llm_cost_path,
        llm_monthly_budget_usd=settings.llm_monthly_budget_usd,
    )

    table = Table(title="실거래 전 안전 체크리스트")
    table.add_column("게이트")
    table.add_column("상태", justify="center")
    table.add_column("상세")
    for r in results:
        if r.skipped:
            status = "[dim]skip[/dim]"
        elif r.ok:
            status = "[green]✓[/green]"
        else:
            status = "[red]✗[/red]"
        name_style = "[dim]" if r.skipped else ""
        name_close = "[/dim]" if r.skipped else ""
        table.add_row(f"{name_style}{r.name}{name_close}", status, r.detail)
    console.print(table)

    failed = [r for r in results if not r.ok and not r.skipped]
    skipped_count = sum(1 for r in results if r.skipped)
    if failed:
        console.print(
            f"[red]✗ {len(failed)}개 게이트 실패 — 위 detail 따라 정리 후 다시 실행.[/red]"
        )
        if skipped_count:
            console.print(f"[dim]({skipped_count}개 게이트 미구현 — 향후 PR)[/dim]")
        raise typer.Exit(code=1)

    console.print(
        "[green]✓ 모든 활성 게이트 통과.[/green]\n"
        "[bold]실거래 전환:[/bold] `.env` 에 실계좌 키 + `KIWOOM_MODE=real` + "
        "`auto-trade paper-day --profile … --allow-real` (사용자 명시 승인 후)."
    )
    if skipped_count:
        console.print(f"[dim]({skipped_count}개 게이트 미구현 — 향후 PR)[/dim]")


@app.command()
def latency(
    path: Path = typer.Option(
        Path("data/latency.jsonl"),
        "--path",
        help="latency 저널 경로.",
    ),
    days: int = typer.Option(
        7,
        "--days",
        help="최근 N일 통계.",
    ),
) -> None:
    """주문 송신 latency 일별 통계 (p50/p95/p99).

    무엇/왜:
    - 실거래에서 백테스트와 PnL 차이의 큰 원인이 latency. 추적해야 식별 가능.
    - 활성: `.env` 의 `LATENCY_LOG_PATH=data/latency.jsonl`. 활성 시 runner 가
      주문 송신마다 자동 누적. 매 호출 비용 < 1ms.
    - p99 가 임계 (예: 3000ms — 키움 API 1회/3.6초 제한 인접) 초과면 단기
      전략 신호가 실거래에서 의미 없어질 위험.

    어떻게 확인: `auto-trade latency --days 7` — 최근 7일 일별 label 통계.
    """
    from datetime import timedelta as _td

    from .monitor.latency import LatencyTracker

    if not path.exists():
        console.print(f"[yellow]latency 저널 없음:[/yellow] {path}")
        console.print("[dim]`.env` 에 LATENCY_LOG_PATH 추가 후 paper-day 1회 이상 시동 필요.[/dim]")
        return

    tracker = LatencyTracker(path=path)
    today = datetime.now(KST).date()

    table = Table(title=f"주문 송신 latency (최근 {days}일)")
    table.add_column("일자")
    table.add_column("label")
    table.add_column("건수", justify="right")
    table.add_column("mean", justify="right")
    table.add_column("p50", justify="right")
    table.add_column("p95", justify="right")
    table.add_column("p99", justify="right")
    table.add_column("max", justify="right")

    any_data = False
    for offset in range(days):
        target = (today - _td(days=offset)).isoformat()
        stats_list = tracker.daily_stats(target)
        if not stats_list:
            continue
        for stats in stats_list:
            any_data = True
            # p99 가 3초 초과면 빨강 강조.
            p99_color = "red" if stats.p99_ms >= 3000 else "white"
            table.add_row(
                stats.date,
                stats.label,
                f"{stats.count:,}",
                f"{stats.mean_ms:.1f}",
                f"{stats.p50_ms:.1f}",
                f"{stats.p95_ms:.1f}",
                f"[{p99_color}]{stats.p99_ms:.1f}[/{p99_color}]",
                f"{stats.max_ms:.1f}",
            )

    if not any_data:
        console.print(f"[dim]최근 {days}일 데이터 없음. paper-day 운영 후 다시 확인.[/dim]")
        return

    console.print(table)
    console.print("[dim]단위: ms · p99 ≥ 3000ms 시 빨강 강조 (단기 전략 신호가 휩쓸릴 위험).[/dim]")


@app.command("detect-regime")
def detect_regime(
    symbol: str = typer.Option(
        "069500",
        "--symbol",
        help="분류 기준 종목. default 069500 (KODEX 200 — KOSPI 200 추종 ETF).",
    ),
    days: int = typer.Option(
        60,
        "--days",
        help="lookback 일봉 수. classify 는 최소 20봉, 60 권장.",
    ),
) -> None:
    """현재 시장국면(추세상승/추세하락/박스/고변동성) 분류.

    무엇/왜:
    - 사용자가 daytrade 프로파일을 선택할 때 시장 상황 한눈에 확인.
    - 추세장은 추세추종(SMA crossover) 이 유리, 박스는 평균회귀(RSI) 가 유리.
    - LLM 호출 0, 외부 의존 키움 일봉 1회.

    어떻게 확인:
    - `auto-trade detect-regime` → KODEX 200 60일봉 기반 분류 출력.
    - `auto-trade detect-regime --symbol 005930 --days 90` 처럼 종목/기간 변경 가능.
    """
    bars = _fetch_daily_bars(symbol)
    df = bars_to_dataframe(bars)
    if len(df) > days:
        df = df.tail(days)

    report = scanner.classify(df, lookback=days)

    color = {
        scanner.MarketRegime.TREND_UP: "green",
        scanner.MarketRegime.TREND_DOWN: "red",
        scanner.MarketRegime.RANGE: "cyan",
        scanner.MarketRegime.HIGH_VOL: "yellow",
    }[report.regime]
    console.print(
        f"[bold {color}]시장국면: {report.regime.value}[/bold {color}]   "
        f"({symbol}, 최근 {int(report.metrics.get('bars', 0))}봉)"
    )
    console.print(f"[dim]{report.summary_ko}[/dim]")

    table = Table(title="지표")
    table.add_column("항목")
    table.add_column("값", justify="right")
    table.add_row("기울기 (일평균 %)", f"{report.metrics.get('slope_pct', 0) * 100:+.3f}")
    table.add_row("ATR / close (%)", f"{report.metrics.get('atr_pct', 0) * 100:.2f}")
    table.add_row("볼린저 밴드폭 (%)", f"{report.metrics.get('bb_width_pct', 0) * 100:.2f}")
    last_close = report.metrics.get("last_close", 0)
    table.add_row("마지막 종가", f"{last_close:,.2f}")
    console.print(table)

    hint = {
        scanner.MarketRegime.TREND_UP: "추세추종(SMA crossover) 유리. 손절을 좀 더 풀어주거나 진입을 적극적으로.",
        scanner.MarketRegime.TREND_DOWN: "하락 추세 — 신규 진입 자제 또는 보수적으로. 매수 비활성 고려.",
        scanner.MarketRegime.RANGE: "박스 — 평균회귀(RSI reversion) 또는 좁은 손절·익절.",
        scanner.MarketRegime.HIGH_VOL: "고변동성 — 신호가 휩쓸릴 위험. 손절 폭을 넓히거나 일시 중지.",
    }[report.regime]
    console.print(f"[bold]권장:[/bold] {hint}")


data_app = typer.Typer(help="데이터 파일 진단·정합성 검사.")
app.add_typer(data_app, name="data")


@data_app.command("check")
def data_check(
    reports_root: Path = typer.Option(
        Path("data/reports"),
        "--reports-root",
        help="daily-*.json 폴더.",
    ),
    journal: Path = typer.Option(
        Path("data/orders/state.jsonl"),
        "--journal",
        help="주문 저널.",
    ),
) -> None:
    """데이터 정합성 일괄 검사 — 깨진 파일이 백테스트/리포트를 오염시키기 전에 잡는다.

    무엇/왜:
    - `data/reports/daily-*.json` — JSON valid + date 필드 + 파일명 일치 + 비정상 PnL% 검출.
    - `data/orders/state.jsonl` — 라인 단위 JSON + event/client_id 필수 + 알려진 event 값.
    - 손상된 파일을 *조기 발견* 하면 디버깅 비용 절약.

    어떻게 확인:
    - 깨끗하면 `✓ 모든 데이터 정합성 통과.` + exit 0.
    - error 1건 이상 → exit 1. warning 만 있으면 exit 0 (정보용).
    """
    from .monitor.data_integrity import run_all_checks, severity_counts

    issues = run_all_checks(reports_root=reports_root, journal_path=journal)
    if not issues:
        console.print("[green]✓ 모든 데이터 정합성 통과.[/green]")
        console.print(f"[dim]검사: reports={reports_root}, journal={journal}[/dim]")
        return

    table = Table(title=f"데이터 정합성 issue ({len(issues)}건)")
    table.add_column("심각도", justify="center")
    table.add_column("종류")
    table.add_column("파일")
    table.add_column("상세")
    for issue in issues:
        color = "red" if issue.severity == "error" else "yellow"
        sev = f"[{color}]{issue.severity}[/{color}]"
        # 파일 경로는 마지막 두 단계만 (긴 절대경로 줄임).
        path_short = "/".join(Path(issue.path).parts[-2:])
        table.add_row(sev, issue.kind, path_short, issue.detail[:80])
    console.print(table)

    err, warn = severity_counts(issues)
    if err > 0:
        console.print(
            f"[red]✗ error {err}건[/red]"
            + (f" + warning {warn}건" if warn else "")
            + " — 데이터 정리 후 다시 실행."
        )
        raise typer.Exit(code=1)
    console.print(f"[yellow]⚠ warning {warn}건 (error 없음)[/yellow]")


@app.command("backtest-compare")
def backtest_compare(
    run1: Path = typer.Argument(..., help="기준 백테스트 결과 폴더 (report.json 포함)."),
    run2: Path = typer.Argument(..., help="비교 대상 폴더 (보통 새로 돌린 결과)."),
    fail_on_regression: bool = typer.Option(
        False,
        "--fail-on-regression",
        help="회귀 감지 시 exit 1 — CI 가드용. 기본은 표시만(exit 0).",
    ),
) -> None:
    """두 백테스트 결과 비교 — Sharpe/PSR/CAGR/MDD/win-rate 차이 표.

    무엇/왜:
    - 전략·파라미터 변경 후 회귀 즉시 감지 (이 PR 후 Sharpe 0.1 떨어졌나? PSR 이 0.9→0.6?).
    - 각 폴더의 `report.json` (engine.save_result 가 저장) 만 읽음. 외부 호출 X.
    - `--fail-on-regression` 으로 CI 가드 가능 (회귀 발견 시 exit 1).

    어떻게 확인:
    - `auto-trade backtest --strategy sma_crossover --symbol 005930 --from 2024-01-01 --out data/backtests/baseline`
    - 코드 변경 후: `--out data/backtests/new`
    - `auto-trade backtest-compare data/backtests/baseline data/backtests/new`
    """
    import json as _json

    def _load(path: Path) -> dict:
        report = path / "report.json"
        if not report.exists():
            console.print(f"[red]report.json 없음:[/red] {report}")
            raise typer.Exit(code=1)
        try:
            return _json.loads(report.read_text(encoding="utf-8"))
        except (OSError, _json.JSONDecodeError) as exc:
            console.print(f"[red]파싱 실패:[/red] {report} — {exc}")
            raise typer.Exit(code=1) from exc

    d1 = _load(run1)
    d2 = _load(run2)

    # 메트릭별 (label, key, higher_is_better, format).
    # higher_is_better=None 이면 색상 X (단순 사실).
    # 29단계 PR31 후속 — Probabilistic Sharpe (PSR) 비교 행 추가.
    metrics: list[tuple[str, str, bool | None, str]] = [
        ("초기 자본", "initial_equity", None, "{:,.0f}"),
        ("최종 자본", "final_equity", None, "{:,.0f}"),
        ("총 수익률 %", "total_return_pct", True, "{:+.2f}"),
        ("CAGR %", "cagr_pct", True, "{:+.2f}"),
        ("Sharpe", "sharpe", True, "{:.3f}"),
        ("Probabilistic Sharpe", "probabilistic_sharpe", True, "{:.3f}"),
        ("연환산 변동성 %", "annualized_vol_pct", False, "{:.2f}"),
        ("최대 낙폭 %", "max_drawdown_pct", True, "{:.2f}"),  # 음수라 큰 값(0에 가까움)이 좋음
        ("승률 %", "win_rate_pct", True, "{:.2f}"),
        ("거래 횟수", "num_trades", None, "{:,}"),
        ("라운드트립", "num_round_trips", None, "{:,}"),
    ]

    table = Table(title=f"백테스트 비교 · {run1.name} → {run2.name}")
    table.add_column("항목")
    table.add_column(run1.name, justify="right")
    table.add_column(run2.name, justify="right")
    table.add_column("차이", justify="right")

    regressions: list[str] = []
    for label, key, higher_better, fmt in metrics:
        v1 = d1.get(key)
        v2 = d2.get(key)
        if v1 is None or v2 is None:
            continue
        diff = float(v2) - float(v1)
        # 부호 명시 — 음수면 fmt 가 자동으로 '-' 붙임, 양수는 '+' 수동 prefix.
        diff_str = ("+" if diff > 0 else "") + fmt.format(diff)
        is_regression = False
        if higher_better is None or abs(diff) < 1e-9:
            color = "white"
        elif (diff > 0) == higher_better:
            color = "green"
        else:
            color = "red"
            is_regression = True
        if is_regression:
            regressions.append(label)
        table.add_row(
            label,
            fmt.format(float(v1)),
            fmt.format(float(v2)),
            f"[{color}]{diff_str}[/{color}]",
        )

    console.print(table)

    if regressions:
        joined = ", ".join(regressions)
        console.print(f"[red]회귀 {len(regressions)}건:[/red] {joined}")
        if fail_on_regression:
            raise typer.Exit(code=1)
    else:
        console.print("[green]✓ 회귀 0건[/green]")


orders_app = typer.Typer(help="주문 저널 진단·복구 도구.")
app.add_typer(orders_app, name="orders")


@orders_app.command("inspect")
def orders_inspect(
    journal: Path = typer.Option(
        Path("data/orders/state.jsonl"),
        "--journal",
        help="주문 저널 경로.",
    ),
    suspect_only: bool = typer.Option(
        False,
        "--suspect-only",
        help="결과 미확인(submit-only) 주문만 표시.",
    ),
    tail: int = typer.Option(
        50,
        "--tail",
        help="최근 N개 이벤트만 (전체 모드). suspect-only 면 무시.",
    ),
) -> None:
    """주문 저널 진단 — 모든 이벤트 표 출력 + 결과 미확인 강조.

    무엇/왜:
    - `preflight` 의 `no_pending_orders` 게이트가 fail 했을 때 *무엇이* 미해결인지
      사용자가 즉시 파악. 키움 HTS / 봇 인증 도구 외부에서 확인 후 정정.
    - 저널은 봇이 송신한 주문의 *진실 기록* — 키움 응답 누락이나 봇 죽음으로
      체결 여부 불확실한 항목 발견에 사용.

    어떻게 확인:
    - `auto-trade orders inspect` — 최근 50개 이벤트 + suspect 강조.
    - `auto-trade orders inspect --suspect-only` — 결과 미확인 항목만.
    """
    from .account.order_journal import OrderJournal
    from .account.recovery import find_suspect_orders

    if not journal.exists():
        console.print(f"[yellow]저널 파일 없음:[/yellow] {journal}")
        console.print(
            "[dim]봇이 한 번도 안 돌았거나 KIWOOM_TOKEN_CACHE_PATH 와 다른 경로일 수 있음.[/dim]"
        )
        return

    j = OrderJournal(path=journal)
    events = list(j.replay())
    if not events:
        console.print(f"[dim]저널 비어있음: {journal}[/dim]")
        return

    suspects = find_suspect_orders(j.replay())
    suspect_ids = {s.client_id for s in suspects}

    if suspect_only:
        if not suspects:
            console.print("[green]✓ 결과 미확인 주문 없음.[/green]")
            return
        table = Table(title=f"결과 미확인 주문 ({len(suspects)}건)")
        table.add_column("client_id (앞 8자)")
        table.add_column("송신 시각")
        table.add_column("종목")
        table.add_column("방향", justify="center")
        table.add_column("수량", justify="right")
        table.add_column("타입")
        table.add_column("가격", justify="right")
        for s in suspects:
            table.add_row(
                s.client_id[:8],
                s.submitted_at,
                s.symbol,
                s.side,
                str(s.quantity),
                s.order_type,
                s.price or "-",
            )
        console.print(table)
        console.print(
            "[yellow]권장 조치:[/yellow] 키움 HTS 에서 해당 client_id 시각·종목·수량과 일치하는 "
            "주문이 *실제로* 들어갔는지 확인 후, 봇 재시작 또는 저널 수동 정리."
        )
        return

    # 전체 모드 — 최근 N개 이벤트 + suspect 강조.
    recent = events[-tail:] if tail > 0 else events
    table = Table(title=f"주문 저널 (최근 {len(recent)}/{len(events)} 이벤트)")
    table.add_column("시각")
    table.add_column("이벤트", justify="center")
    table.add_column("client_id")
    table.add_column("상세")

    for ev in recent:
        ts = str(ev.get("ts", ""))[:19].replace("T", " ")
        kind = str(ev.get("event", "?"))
        cid_full = str(ev.get("client_id", ""))
        cid = cid_full[:8]
        is_suspect = cid_full in suspect_ids and kind == "submit"

        if kind == "submit":
            symbol = ev.get("symbol", "?")
            side = ev.get("side", "?")
            qty = ev.get("qty", "?")
            detail = f"{side} {symbol} x{qty}"
            if is_suspect:
                detail = f"[red]{detail} (결과 미확인)[/red]"
            kind_label = "[yellow]submit[/yellow]" if is_suspect else "submit"
        elif kind == "accepted":
            detail = f"order_no={ev.get('order_no', '?')}"
            kind_label = "[green]accepted[/green]"
        elif kind == "failed":
            err = str(ev.get("error", ""))[:60]
            detail = f"[red]{err}[/red]"
            kind_label = "[red]failed[/red]"
        else:
            detail = str(ev)[:60]
            kind_label = kind

        table.add_row(ts, kind_label, cid, detail)

    console.print(table)
    if suspects:
        console.print(
            f"[yellow]주의:[/yellow] 결과 미확인 주문 {len(suspects)}건. "
            "`auto-trade orders inspect --suspect-only` 로 상세 확인."
        )
    else:
        console.print("[green]✓ 결과 미확인 주문 없음.[/green]")


if __name__ == "__main__":
    app()
