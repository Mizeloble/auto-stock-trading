"""`auto-trade` CLI 엔트리.

명령:
- `quote <symbol>`  — 현재가
- `balance`         — 계좌 잔고
- `backtest`        — 과거 데이터 시뮬레이션
- `paper`           — 모의투자 실시간 매매 루프
"""

from __future__ import annotations

import asyncio
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
from .runner import PaperRunner
from .strategy import registry

app = typer.Typer(help="키움 자동매매 봇 CLI (모의투자 기본)")
console = Console()


def _setup_logging() -> None:
    level = getattr(logging, get_settings().log_level.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
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
            for col in ("코드", "종목명", "수량", "평단가", "현재가", "평가금액", "손익", "수익률%"):
                holdings.add_column(col, justify="right" if col != "종목명" else "left")
            for h in snap.holdings:
                holdings.add_row(
                    h.symbol, h.name,
                    f"{h.quantity:,}", f"{h.avg_price:,}",
                    f"{h.current_price:,}", f"{h.market_value:,}",
                    f"{h.pnl:+,}", f"{h.pnl_pct:+}%",
                )
            console.print(holdings)

    asyncio.run(_run())


@app.command()
def backtest(
    strategy: str = typer.Option(..., "--strategy", "-s", help="전략 이름 (예: sma_crossover)"),
    symbol: str = typer.Option(..., "--symbol", help="종목코드"),
    start: str = typer.Option(..., "--from", help="YYYY-MM-DD"),
    end: str | None = typer.Option(None, "--to", help="YYYY-MM-DD, 생략 시 오늘"),
    cash: float = typer.Option(10_000_000.0, "--cash", help="초기 예수금"),
    out_dir: Path | None = typer.Option(None, "--out", help="결과 저장 폴더"),
) -> None:
    """과거 일봉으로 전략 시뮬레이션."""

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

    strat = registry.get(strategy)
    bt = Backtester(strategy=strat, initial_cash=cash)
    result = bt.run(symbol, df)

    rep = result.report.as_dict()
    table = Table(title=f"백테스트 결과 · {strategy} · {symbol}")
    table.add_column("지표")
    table.add_column("값", justify="right")
    for k, v in rep.items():
        table.add_row(k, str(v))
    console.print(table)

    out = out_dir or Path("data/backtests") / f"{strategy}_{symbol}_{datetime.now():%Y%m%d_%H%M%S}"
    saved = save_result(result, out)
    console.print(f"[green]결과 저장:[/green] {saved}")


@app.command()
def paper(
    strategy: str = typer.Option(..., "--strategy", "-s", help="전략 이름"),
    symbols: str = typer.Option(..., "--symbols", help="콤마 구분 종목코드"),
    minutes: int = typer.Option(1, "--minutes", help="집계 봉 길이(분)"),
    allow_real: bool = typer.Option(False, "--allow-real", help="실거래에서도 돌릴지 (위험)"),
) -> None:
    """실시간 틱을 받아 전략 실행. 모의투자 기본."""
    strat = registry.get(strategy)
    symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]
    if not symbol_list:
        raise typer.BadParameter("symbols 가 비었습니다")

    runner = PaperRunner(
        strategy=strat,
        symbols=symbol_list,
        bar_minutes=minutes,
        allow_real=allow_real,
    )
    try:
        asyncio.run(runner.run())
    except KeyboardInterrupt:
        console.print("[yellow]사용자 중단.[/yellow]")


if __name__ == "__main__":
    app()
