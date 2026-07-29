"""운영 진단 명령: dashboard / heartbeat-check / preflight / latency / data / orders / ops."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import typer
from rich.table import Table

from auto_trading import cli

from .._common import KST
from ..heartbeat import (
    DEFAULT_COOLDOWN_PATH,
    DEFAULT_COOLDOWN_SEC,
    DEFAULT_HEARTBEAT_PATH,
    DEFAULT_MAX_AGE_SEC,
)


@cli.app.command()
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

    from ..dashboard.app import create_app

    instance = create_app(reports_root=reports_root, backtests_root=backtests_root)
    cli.console.print(f"[cyan]대시보드:[/cyan] http://{host}:{port}")
    cli.console.print(
        f"[dim]reports={reports_root.resolve()} · backtests={backtests_root.resolve()}[/dim]"
    )
    uvicorn.run(instance, host=host, port=port, log_level="info")


@cli.app.command("heartbeat-check")
def heartbeat_check(
    path: Path = typer.Option(
        DEFAULT_HEARTBEAT_PATH,
        "--path",
        help="러너가 갱신하는 heartbeat 파일.",
    ),
    max_age_sec: int = typer.Option(
        DEFAULT_MAX_AGE_SEC,
        "--max-age-sec",
        help="이 초 이상 갱신 누락이면 'stale' 로 판정.",
    ),
    cooldown_sec: int = typer.Option(
        DEFAULT_COOLDOWN_SEC,
        "--cooldown-sec",
        help="같은 누락 상황에서 텔레그램 알림 최소 간격 (spam 방지).",
    ),
    cooldown_path: Path = typer.Option(
        DEFAULT_COOLDOWN_PATH,
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
    from ..heartbeat import DEFAULT_STOPPED_PATH, check_heartbeat

    # 29단계 회귀(2026-05-11) — 러너가 정상 종료 sentinel 을 남기면 cron 은 침묵.
    # heartbeat 파일과 같은 디렉터리에 있는 stopped.txt 를 본다.
    stopped_path = path.parent / DEFAULT_STOPPED_PATH.name
    status = check_heartbeat(path, max_age_sec, stopped_path=stopped_path)
    if status.ok:
        if status.reason == "stopped":
            cli.console.print(
                f"[green]heartbeat stopped[/green] · sentinel ok (path={stopped_path})"
            )
        else:
            cli.console.print(
                f"[green]heartbeat OK[/green] · age={status.age_sec:.0f}s (path={path})"
            )
        raise typer.Exit(code=0)

    # cooldown — 직전 알림이 cooldown_sec 안이면 침묵.
    now_ts = datetime.now().timestamp()
    if cooldown_path.exists():
        since_alert = now_ts - cooldown_path.stat().st_mtime
        if since_alert < cooldown_sec:
            cli.console.print(
                f"[yellow]heartbeat {status.reason}[/yellow] — cooldown 중 "
                f"({since_alert:.0f}s/{cooldown_sec}s, 알림 생략)"
            )
            raise typer.Exit(code=0)

    cooldown_path.parent.mkdir(parents=True, exist_ok=True)
    cooldown_path.touch()

    cli.console.print(f"[red]heartbeat {status.reason}[/red] (path={path})")
    notifier = cli.build_notifier()

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


@cli.app.command()
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
            cli.console.print(f"[dim]{profile} 없음 → fallback {fallback}[/dim]")
            profile = fallback

    from ..preflight import run_all_gates

    settings = cli.get_settings()
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
    cli.console.print(table)

    failed = [r for r in results if not r.ok and not r.skipped]
    skipped_count = sum(1 for r in results if r.skipped)
    if failed:
        cli.console.print(
            f"[red]✗ {len(failed)}개 게이트 실패 — 위 detail 따라 정리 후 다시 실행.[/red]"
        )
        if skipped_count:
            cli.console.print(f"[dim]({skipped_count}개 게이트 미구현 — 향후 PR)[/dim]")
        raise typer.Exit(code=1)

    cli.console.print(
        "[green]✓ 모든 활성 게이트 통과.[/green]\n"
        "[bold]실거래 전환:[/bold] `.env` 에 실계좌 키 + `KIWOOM_MODE=real` + "
        "`auto-trade paper-day --profile … --allow-real` (사용자 명시 승인 후)."
    )
    if skipped_count:
        cli.console.print(f"[dim]({skipped_count}개 게이트 미구현 — 향후 PR)[/dim]")


@cli.app.command()
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

    from ..monitor.latency import LatencyTracker

    if not path.exists():
        cli.console.print(f"[yellow]latency 저널 없음:[/yellow] {path}")
        cli.console.print(
            "[dim]`.env` 에 LATENCY_LOG_PATH 추가 후 paper-day 1회 이상 시동 필요.[/dim]"
        )
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
        cli.console.print(f"[dim]최근 {days}일 데이터 없음. paper-day 운영 후 다시 확인.[/dim]")
        return

    cli.console.print(table)
    cli.console.print(
        "[dim]단위: ms · p99 ≥ 3000ms 시 빨강 강조 (단기 전략 신호가 휩쓸릴 위험).[/dim]"
    )


@cli.data_app.command("check")
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
    from ..monitor.data_integrity import run_all_checks, severity_counts

    issues = run_all_checks(reports_root=reports_root, journal_path=journal)
    if not issues:
        cli.console.print("[green]✓ 모든 데이터 정합성 통과.[/green]")
        cli.console.print(f"[dim]검사: reports={reports_root}, journal={journal}[/dim]")
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
    cli.console.print(table)

    err, warn = severity_counts(issues)
    if err > 0:
        cli.console.print(
            f"[red]✗ error {err}건[/red]"
            + (f" + warning {warn}건" if warn else "")
            + " — 데이터 정리 후 다시 실행."
        )
        raise typer.Exit(code=1)
    cli.console.print(f"[yellow]⚠ warning {warn}건 (error 없음)[/yellow]")


@cli.orders_app.command("inspect")
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
    from ..account.order_journal import OrderJournal
    from ..account.recovery import find_suspect_orders

    if not journal.exists():
        cli.console.print(f"[yellow]저널 파일 없음:[/yellow] {journal}")
        cli.console.print(
            "[dim]봇이 한 번도 안 돌았거나 KIWOOM_TOKEN_CACHE_PATH 와 다른 경로일 수 있음.[/dim]"
        )
        return

    j = OrderJournal(path=journal)
    events = list(j.replay())
    if not events:
        cli.console.print(f"[dim]저널 비어있음: {journal}[/dim]")
        return

    suspects = find_suspect_orders(j.replay())
    suspect_ids = {s.client_id for s in suspects}

    if suspect_only:
        if not suspects:
            cli.console.print("[green]✓ 결과 미확인 주문 없음.[/green]")
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
        cli.console.print(table)
        cli.console.print(
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

    cli.console.print(table)
    if suspects:
        cli.console.print(
            f"[yellow]주의:[/yellow] 결과 미확인 주문 {len(suspects)}건. "
            "`auto-trade orders inspect --suspect-only` 로 상세 확인."
        )
    else:
        cli.console.print("[green]✓ 결과 미확인 주문 없음.[/green]")


# ---------------------------------------------------------------------------
# `ops` — 8개 LaunchAgent 자동실행 작업 상태 한눈에 (Phase 1 가시성 도구)
# ---------------------------------------------------------------------------


@cli.app.command()
def ops(
    launchd_dir: Path = typer.Option(
        Path("scripts/launchd"),
        "--launchd-dir",
        help="`com.mizeloble.auto-trade.*.plist` 가 있는 폴더.",
    ),
    logs_dir: Path = typer.Option(
        Path("logs"),
        "--logs-dir",
        help="`launchd-*.{out,err}.log` 가 쌓이는 폴더.",
    ),
) -> None:
    """8개 LaunchAgent 자동실행 작업 상태 한눈에.

    무엇/왜:
    - macOS launchd 에 등록된 자동매매 봇 자동실행 작업이 의도대로 도는지 한 줄씩 확인.
    - "마지막 실행" = 레거시 `logs/launchd-*.{out,err}.log` 와 래퍼 스크립트의
      날짜별 `logs/<잡이름>-YYYY-MM-DD.log` 를 합쳐 최신 mtime. (2026-07-12 수정 —
      종전엔 레거시만 봐서 대부분 잡이 5월에 고착돼 보였다.)
    - "다음 예정" = plist 의 StartCalendarInterval 시뮬레이션. StartInterval 잡은
      "매 5분" 식 간격 표기.
    - "7일 실행일" = 예상/실제. 예상=스케줄상 발화 예정일 수(어제까지 7일),
      실제=날짜별 로그가 생긴 날 수. 실제 < 예상이면 노랑 — 놓친 날이 있다는 뜻.
      날짜별 로그가 없는 잡(heartbeat-check·log-rotate)은 실제 측정 불가 "-".
    - "Exit" = `launchctl print gui/$UID/<label>` 출력의 `last exit code` 1줄 grep:
      "0" 정상 종료, 빨강 정수 비정상 종료, "·" 이번 부팅 후 미실행
      (launchctl 의 `(never exited)`), "-" launchctl 호출 자체 실패.

    어떻게 확인: `uv run auto-trade ops` — 즉시 표 출력. churnfix-verify 같은 만료된
    1회성 작업은 비고에 "⚠ 만료된 1회성, 별도 정리" 표시.
    """
    from ..monitor.launchd import collect_launchd_status

    rows = collect_launchd_status(launchd_dir, logs_dir)
    if not rows:
        cli.console.print(
            f"[yellow]등록된 LaunchAgent plist 가 없습니다.[/yellow] (dir={launchd_dir})"
        )
        return

    table = Table(title="자동실행 작업 현황")
    table.add_column("Label")
    table.add_column("마지막 실행")
    table.add_column("다음 예정")
    table.add_column("7일 실행일", justify="center")
    table.add_column("Exit", justify="right")
    table.add_column("비고")
    for r in rows:
        short = r.label.removeprefix("com.mizeloble.auto-trade.")
        last = r.last_run.strftime("%Y-%m-%d %H:%M") if r.last_run else "-"

        if r.next_run is not None:
            # 올해 발화는 "07-13 08:55", 내년(연 1회 잡)은 연도까지 — 과거로 오독 방지.
            fmt = "%m-%d %H:%M" if r.next_run.year == datetime.now(KST).year else "%Y-%m-%d %H:%M"
            next_cell = r.next_run.strftime(fmt)
        elif r.interval_sec is not None:
            next_cell = _format_interval(r.interval_sec)
        else:
            next_cell = "-"

        if r.expected_days_7d is None:
            week_cell = "-"
        else:
            actual = "-" if r.actual_days_7d is None else str(r.actual_days_7d)
            week_cell = f"{r.expected_days_7d}/{actual}"
            if r.actual_days_7d is not None and r.actual_days_7d < r.expected_days_7d:
                week_cell = f"[yellow]{week_cell}[/yellow]"

        if r.exit_status is None:
            exit_cell = "-"
        elif r.exit_status == "0":
            exit_cell = "0"
        elif r.exit_status == "(never exited)":
            exit_cell = "·"
        else:
            exit_cell = f"[red]{r.exit_status}[/red]"
        table.add_row(short, last, next_cell, week_cell, exit_cell, r.note)
    cli.console.print(table)
    cli.console.print(
        "[dim]Exit: 0=정상 · 빨강=비정상 · ·=이번 부팅 후 미실행 · -=정보 없음 | "
        "7일 실행일: 예상/실제(어제까지 7일 · 노랑=놓친 날 있음 · -=날짜별 로그 없어 측정 불가)[/dim]"
    )


def _format_interval(sec: int) -> str:
    """StartInterval 초를 사람이 읽는 간격으로 (300 → "매 5분")."""
    if sec % 3600 == 0:
        return f"매 {sec // 3600}시간"
    if sec % 60 == 0:
        return f"매 {sec // 60}분"
    return f"매 {sec}초"
