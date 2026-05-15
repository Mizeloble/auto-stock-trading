"""리포트·LLM·뉴스 명령: report / insight / weekly-proposal / fetch-news / detect-regime."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import typer
from rich.table import Table

from auto_trading import cli

from .._common import KST
from ..news.cli_helpers import (
    WeeklyInputs,
    run_weekly_proposal_cycle,
    should_skip_weekly_proposal_for_holiday,
)
from ..notify import insight as insight_mod
from ..notify.state import format_daily_report
from ..reports.daily import build_daily_report, save_daily_report
from ..reports.log_parser import parse_log
from ..strategy import scanner


@cli.app.command()
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
        cli.console.print(f"[red]로그 파일 없음:[/red] {log_file}")
        raise typer.Exit(code=1)

    events = parse_log(log_file)
    daily = build_daily_report(date=iso, events=events, snapshot=None)
    save_path = save_daily_report(daily, root=out)
    cli.console.print(f"[green]리포트 저장:[/green] {save_path}")

    # asdict 으로 신규 필드 자동 포함. discovery_blocked tuple 만 list 로 호환 변환.
    payload = asdict(daily)
    payload["discovery_blocked"] = [list(t) for t in daily.discovery_blocked]
    cli.console.print(format_daily_report(payload))

    if push:
        notifier = cli.build_notifier()

        async def _push() -> None:
            try:
                await notifier.notify("runner.daily_report", **payload)
            finally:
                await notifier.aclose()

        asyncio.run(_push())
        cli.console.print("[green]텔레그램 푸시 완료[/green]")


@cli.app.command()
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
        cli.console.print(
            f"[red]리포트 데이터 부족:[/red] {reports_root}/daily-*.json 을 찾지 못했습니다. "
            "먼저 `auto-trade report --date YYYY-MM-DD` 로 일일 리포트를 생성하세요."
        )
        raise typer.Exit(code=1)

    date_range = (reports[0].date, reports[-1].date)
    cli.console.print(f"[cyan]입력:[/cyan] {len(reports)}일 ({date_range[0]}~{date_range[1]})")

    if dry_run:
        from ..notify.insight import _summarize_reports

        cli.console.print(_summarize_reports(reports))
        return

    client = cli._build_anthropic_client()
    response = insight_mod.insight(
        reports,
        client=client,  # type: ignore[arg-type]
        model=cli.get_settings().anthropic_model,
    )
    msg = insight_mod.format_insight_message(response, days=len(reports), date_range=date_range)
    cli.console.print(msg)

    if push:
        notifier = cli.build_notifier()

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
        cli.console.print("[green]텔레그램 푸시 완료[/green]")


@cli.app.command("weekly-proposal")
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
    skip_msg = should_skip_weekly_proposal_for_holiday(
        force=force, today=cli.krx_calendar.today_kst()
    )
    if skip_msg is not None:
        cli.console.print(f"[yellow]📅 {skip_msg}[/yellow]")
        raise typer.Exit(code=0)

    from ..news.collector import collect_recent_news
    from ..news.proposal import _summarize_news, _summarize_reports

    inputs = WeeklyInputs(
        reports=list(insight_mod.collect_recent_reports(days=days_reports)),
        news=list(collect_recent_news(days=days_news, force_refresh=force_refresh)),
    )
    if inputs.is_empty:
        cli.console.print("[red]데이터 부족:[/red] reports + news 모두 비어있습니다.")
        raise typer.Exit(code=1)

    # 29단계 PR18 (#5 일부) — 현재 시장국면을 LLM 입력에 추가.
    # KOSPI 200 추종 ETF(069500) 60일봉 → classify. fetch 실패는 swallow (regime=None
    # 으로 LLM 호출 — 기존 동작 유지). LLM 비용 절약 위해 dry-run 도 시도.
    regime: scanner.RegimeReport | None = None
    try:
        regime_bars = cli._fetch_daily_bars("069500")
        regime_df = cli.bars_to_dataframe(regime_bars).tail(60)
        regime = scanner.classify(regime_df, lookback=60)
        cli.console.print(f"[cyan]시장국면:[/cyan] {regime.regime.value} — {regime.summary_ko}")
    except Exception as exc:
        cli.console.print(f"[dim]regime 분류 생략 (fetch 실패): {exc}[/dim]")

    cli.console.print(
        f"[cyan]입력:[/cyan] reports {len(inputs.reports)}건 · news {len(inputs.news)}건 "
        f"(LLM 입력 최대 {max_news_items}건)"
    )

    if dry_run:
        cli.console.print(
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
        client=cli._build_anthropic_client(),
        model=cli.get_settings().anthropic_model,
        max_news_items=max_news_items,
        regime=regime,
    )
    cli.console.print(msg)

    if push:
        notifier = cli.build_notifier()

        async def _push() -> None:
            try:
                await notifier.notify("runner.weekly_proposal", text=msg)
            finally:
                await notifier.aclose()

        asyncio.run(_push())
        cli.console.print("[green]텔레그램 푸시 완료[/green]")


@cli.app.command("fetch-news")
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
    from ..news.collector import collect_recent_news

    news = collect_recent_news(days=days, force_refresh=force_refresh)
    sources: dict[str, int] = {}
    for it in news:
        sources[it.source] = sources.get(it.source, 0) + 1
    cli.console.print(f"[cyan]뉴스 수집:[/cyan] {len(news)}건 (직전 {days}일)")
    for src, cnt in sorted(sources.items(), key=lambda kv: -kv[1]):
        cli.console.print(f"  · {src}: {cnt}건")


@cli.app.command("detect-regime")
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
    bars = cli._fetch_daily_bars(symbol)
    df = cli.bars_to_dataframe(bars)
    if len(df) > days:
        df = df.tail(days)

    report = scanner.classify(df, lookback=days)

    color = {
        scanner.MarketRegime.TREND_UP: "green",
        scanner.MarketRegime.TREND_DOWN: "red",
        scanner.MarketRegime.RANGE: "cyan",
        scanner.MarketRegime.HIGH_VOL: "yellow",
    }[report.regime]
    cli.console.print(
        f"[bold {color}]시장국면: {report.regime.value}[/bold {color}]   "
        f"({symbol}, 최근 {int(report.metrics.get('bars', 0))}봉)"
    )
    cli.console.print(f"[dim]{report.summary_ko}[/dim]")

    table = Table(title="지표")
    table.add_column("항목")
    table.add_column("값", justify="right")
    table.add_row("기울기 (일평균 %)", f"{report.metrics.get('slope_pct', 0) * 100:+.3f}")
    table.add_row("ATR / close (%)", f"{report.metrics.get('atr_pct', 0) * 100:.2f}")
    table.add_row("볼린저 밴드폭 (%)", f"{report.metrics.get('bb_width_pct', 0) * 100:.2f}")
    last_close = report.metrics.get("last_close", 0)
    table.add_row("마지막 종가", f"{last_close:,.2f}")
    cli.console.print(table)

    hint = {
        scanner.MarketRegime.TREND_UP: "추세추종(SMA crossover) 유리. 손절을 좀 더 풀어주거나 진입을 적극적으로.",
        scanner.MarketRegime.TREND_DOWN: "하락 추세 — 신규 진입 자제 또는 보수적으로. 매수 비활성 고려.",
        scanner.MarketRegime.RANGE: "박스 — 평균회귀(RSI reversion) 또는 좁은 손절·익절.",
        scanner.MarketRegime.HIGH_VOL: "고변동성 — 신호가 휩쓸릴 위험. 손절 폭을 넓히거나 일시 중지.",
    }[report.regime]
    cli.console.print(f"[bold]권장:[/bold] {hint}")
