"""전략 추천·프로파일 관리 명령: strategy advise / strategy suggest."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table

from auto_trading import cli

from ..strategy import scanner, suggest
from ..strategy.profile import StrategyProfile


@cli.strategy_app.command("advise")
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

    from .._common import KST
    from ..strategy import llm_advisor as advisor

    client = cli._build_anthropic_client()
    prof = StrategyProfile.load_yaml(profile)

    bars = cli._fetch_daily_bars(prof.symbol, max_pages=2)
    df = cli.bars_to_dataframe(bars)
    report = scanner.classify(df, lookback=lookback)
    response = advisor.advise(
        prof,
        df,
        report,
        client=client,  # type: ignore[arg-type]
        model=cli.get_settings().anthropic_model,
    )

    table = Table(title=f"Advisor 제안 · {prof.symbol}")
    table.add_column("항목")
    table.add_column("기존")
    table.add_column("제안")
    table.add_row("strategy", prof.strategy, response.change.strategy)
    table.add_row("params", str(prof.params), str(response.change.params))
    table.add_row("weight", f"{prof.weight}", f"{response.change.weight}")
    cli.console.print(table)
    cli.console.print(f"[dim]근거:[/dim] {response.reason_ko}")
    cli.console.print(f"[dim]confidence:[/dim] {response.confidence:.2f}")

    no_change = (
        prof.strategy == response.change.strategy
        and prof.params == response.change.params
        and prof.weight == response.change.weight
    )
    if no_change:
        cli.console.print("[yellow]변경 없음 — 파일·백업 모두 그대로 둡니다.[/yellow]")
        return
    if dry_run:
        cli.console.print("[yellow]--dry-run 모드: 파일을 수정하지 않습니다.[/yellow]")
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
    cli.console.print(f"[green]프로파일 갱신:[/green] {profile} (백업: {backup.name})")


@cli.strategy_app.command("suggest")
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

    bars = cli._fetch_daily_bars(symbol, max_pages=2)
    df = cli.bars_to_dataframe(bars)
    report = scanner.classify(df, lookback=lookback)
    profile = suggest.recommend(report, symbol)

    table = Table(title=f"시장국면 · {symbol}")
    table.add_column("항목")
    table.add_column("값", justify="right")
    table.add_row("국면", report.regime.value)
    for k, v in report.metrics.items():
        table.add_row(k, f"{v:,.4f}" if isinstance(v, float) else str(v))
    cli.console.print(table)
    cli.console.print(f"[dim]요약:[/dim] {report.summary_ko}")
    cli.console.print(
        f"[green]추천 전략:[/green] {profile.strategy} "
        f"params={profile.params} weight={profile.weight}"
    )

    out_path = out / f"auto_{symbol}.yaml"
    profile.dump_yaml(out_path)
    cli.console.print(f"[green]프로파일 저장:[/green] {out_path}")
    cli.console.print(
        f"[dim]다음 단계:[/dim] uv run auto-trade backtest --profile {out_path} --from 2025-01-01"
    )
