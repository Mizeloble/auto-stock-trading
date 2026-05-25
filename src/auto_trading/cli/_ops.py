"""운영 진단 명령: dashboard / heartbeat-check / preflight / latency / data / orders."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import typer
from rich.table import Table

from auto_trading import cli

from .._common import KST


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


@cli.app.command("build-marketcap-universe")
def build_marketcap_universe(
    top_n: int = typer.Option(200, "--top-n", help="상위 몇 개 종목까지 마스터에 저장할지."),
    out_dir: Path = typer.Option(
        Path("data/snapshots"),
        "--out-dir",
        help="`universe-YYYY-MM-DD.json` 저장 폴더.",
    ),
) -> None:
    """KRX 시가총액 상위 N 종목 마스터를 산출해 JSON 캐시에 저장.

    무엇/왜:
    - `snapshot-top200` 잡이 매 10분 호출할 *종목 풀*. 매일 1회 (08:50 KST) 갱신.
    - 키움 API 에 시총 정렬 TR 이 없어 `pykrx` 로 KRX 공식 시총을 받아옴.
    - 출력: `data/snapshots/universe-YYYY-MM-DD.json` (오늘 KST 날짜 기준 파일명,
      데이터는 그 시점 직전 거래일 종가 기준).

    어떻게 확인:
    - `cat data/snapshots/universe-YYYY-MM-DD.json | python -m json.tool | head`.
    - rank 1 이 보통 삼성전자, market_cap desc 정렬.
    """
    from ..market.marketcap import (
        compute_top_marketcap_universe,
        save_universe,
        universe_path_for,
    )

    entries = compute_top_marketcap_universe(top_n=top_n)
    if not entries:
        cli.console.print(
            "[red]시총 데이터를 받지 못했습니다.[/red] "
            "KRX 페이지 일시 장애이거나 pykrx 응답 형식이 바뀌었을 수 있습니다."
        )
        raise typer.Exit(code=1)

    path = universe_path_for(out_dir)
    save_universe(entries, path, top_n=top_n)
    cli.console.print(f"[green]✓ 시총 마스터 저장:[/green] {path} ({len(entries)}건)")
    cli.console.print(
        f"[dim]1위: {entries[0].symbol} {entries[0].name} · "
        f"시총 {entries[0].market_cap / 1e12:.1f}조[/dim]"
    )


@cli.app.command("snapshot-top200")
def snapshot_top200(
    universe_dir: Path = typer.Option(
        Path("data/snapshots"),
        "--universe-dir",
        help="`universe-YYYY-MM-DD.json` 가 있는 폴더.",
    ),
    out_dir: Path = typer.Option(
        Path("data/snapshots"),
        "--out-dir",
        help="`topcap-YYYY-MM-DD.jsonl` 가 append 될 폴더.",
    ),
    fallback_to_yesterday: bool = typer.Option(
        True,
        "--fallback-to-yesterday/--no-fallback-to-yesterday",
        help="오늘 universe 가 없으면 어제 캐시로 폴백.",
    ),
    max_fallback_days: int = typer.Option(
        7,
        "--max-fallback-days",
        help="폴백 시 거슬러 올라갈 최대 일수.",
    ),
) -> None:
    """시총 상위 200 종목 현재가 1라운드 스냅샷 → JSONL append.

    무엇/왜:
    - launchd 가 매 10분 (평일 09:00-15:30 KST) 호출 → 약 30초간 200종목 `ka10001`
      호출 → `data/snapshots/topcap-YYYY-MM-DD.jsonl` 에 append.
    - universe 가 없으면 어제(또는 그 전 거래일) 캐시로 폴백 — 마스터 잡이 늦거나
      실패한 경우에도 스냅샷은 계속 쌓이도록.

    어떻게 확인:
    - 1회 실행: `uv run auto-trade snapshot-top200` → 약 30초 후 종료.
    - `wc -l data/snapshots/topcap-YYYY-MM-DD.jsonl` 가 라운드당 ~200 증가.
    - `head -1 … | python -m json.tool` 로 스키마 확인.
    """
    from datetime import timedelta as _td

    from ..market.marketcap import load_cached_universe, universe_path_for
    from ..monitor.price_snapshot import (
        IndexSnapshotRecorder,
        PriceSnapshotRecorder,
        index_snapshot_path_for,
        snapshot_path_for,
    )

    # 1) universe 로드 (오늘 → 어제 → … 최대 max_fallback_days).
    today = datetime.now(KST).date()
    universe = []
    chosen_path: Path | None = None
    for offset in range(max_fallback_days + 1):
        target = today - _td(days=offset)
        cand = universe_path_for(universe_dir, when=target)
        if cand.exists():
            universe = load_cached_universe(cand)
            if universe:
                chosen_path = cand
                if offset > 0 and not fallback_to_yesterday:
                    cli.console.print("[yellow]오늘 universe 없음 — 폴백 비활성이라 종료.[/yellow]")
                    raise typer.Exit(code=1)
                break

    if not universe:
        cli.console.print(
            "[red]universe 캐시를 찾지 못했습니다.[/red] "
            "먼저 `auto-trade build-marketcap-universe --top-n 200` 으로 마스터를 만드세요."
        )
        raise typer.Exit(code=1)

    if chosen_path and chosen_path.name != f"universe-{today.isoformat()}.json":
        cli.console.print(f"[yellow]오늘 universe 없음 — 폴백 사용:[/yellow] {chosen_path.name}")

    out_path = snapshot_path_for(out_dir)
    index_path = index_snapshot_path_for(out_dir)
    recorder = PriceSnapshotRecorder(path=out_path)
    index_recorder = IndexSnapshotRecorder(path=index_path)

    async def _run() -> tuple[int, int]:
        async with cli.KiwoomRestClient() as client:
            entries = await recorder.record_round(client, universe)
            indices = await index_recorder.record_round(client)
            return len(entries), len(indices)

    ok, idx_ok = asyncio.run(_run())
    cli.console.print(f"[green]✓ 스냅샷 1라운드:[/green] 종목 {ok}/{len(universe)}건 → {out_path}")
    cli.console.print(f"[green]✓ 지수 스냅샷:[/green] {idx_ok}/2건 (KOSPI/KOSDAQ) → {index_path}")
