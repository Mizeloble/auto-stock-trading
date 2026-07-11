"""시장 데이터 수집 잡: build-marketcap-universe / snapshot-top200 / vi.

launchd 가 주기 호출하는 read-only 수집·관찰 도구 — 매매 경로와 무관.
운영 진단 명령(dashboard/preflight/ops 등)은 `_ops.py`.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from datetime import time as dtime
from pathlib import Path
from typing import cast

import structlog
import typer
from rich.table import Table

from auto_trading import cli

from .._common import KST

# snapshot-top200 은 launchd 가 매 10분(09:00~15:30) 호출 — 이 시각 이후의 호출
# (= 15:30 마지막 라운드)에서만 일일 리포트 조인 재보강을 수행한다.
_REPORT_REFRESH_AFTER = dtime(15, 25)


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

    from ..daytrade.cli_helpers import ALT_RANK_PATH
    from ..market.marketcap import load_cached_universe, universe_path_for
    from ..monitor.price_snapshot import (
        IndexSnapshotRecorder,
        PriceSnapshotRecorder,
        discovered_snapshot_path_for,
        index_snapshot_path_for,
        load_todays_discovery_symbols,
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

    # 오늘 발굴 종목(거래량 픽/풀 + 거래대금 풀) 중 시총 200 밖 종목 — 별도 파일에
    # 함께 스냅샷해 발굴 소형주도 일중 데이터를 갖게 한다(topcap 은 breadth 게이트가
    # 줄 수를 세므로 섞지 않음). alt-rank 미기록일(휴장·발굴 전)은 빈 리스트 = no-op.
    discovered = load_todays_discovery_symbols(
        ALT_RANK_PATH, exclude_symbols={e.symbol for e in universe}
    )
    discovered_path = discovered_snapshot_path_for(out_dir)

    async def _run() -> tuple[int, int, int]:
        # 200종목을 10분 윈도우 안에 천천히 스윕 — 기본 0.15s 페이싱은 키움 ka10001
        # 초당 한도보다 빨라 거의 모든 호출이 429→재시도였다(2026-06-17 관측: 하루
        # ~7,600회 헛재시도). 매매 루프와 달리 시간 여유가 크므로 0.5s 로 늦춰 버스트
        # 자체를 없앤다. 200콜 x 0.5s ≈ 100s 로 10분 윈도우에 넉넉.
        async with cli.KiwoomRestClient(min_request_interval_sec=0.5) as client:
            entries = await recorder.record_round(client, list(universe))
            indices = await index_recorder.record_round(client)
            disc_count = 0
            if discovered:
                try:
                    disc_recorder = PriceSnapshotRecorder(path=discovered_path)
                    disc_entries = await disc_recorder.record_round(client, list(discovered))
                    disc_count = len(disc_entries)
                except Exception:
                    # 발굴 스냅샷은 부가 데이터 — 실패가 본 라운드 결과를 바꾸면 안 된다.
                    structlog.get_logger(__name__).warning(
                        "snapshot.discovered_round_failed", exc_info=True
                    )
            return len(entries), len(indices), disc_count

    ok, idx_ok, disc_ok = asyncio.run(_run())
    cli.console.print(f"[green]✓ 스냅샷 1라운드:[/green] 종목 {ok}/{len(universe)}건 → {out_path}")
    cli.console.print(f"[green]✓ 지수 스냅샷:[/green] {idx_ok}/2건 (KOSPI/KOSDAQ) → {index_path}")
    if discovered:
        cli.console.print(
            f"[green]✓ 발굴 종목 스냅샷:[/green] {disc_ok}/{len(discovered)}건 → {discovered_path}"
        )

    # 마지막 라운드(15:30 호출)에서 오늘 일일 리포트의 스냅샷 조인을 소급 갱신 —
    # 게이트 차단일엔 러너가 09:02 에 리포트를 저장해 차단 종목 성적(blocked_outcomes)
    # 이 개장 1라운드 값으로 얼어붙는다. 하루치 스냅샷이 다 모인 지금 다시 채운다.
    # 실패해도 스냅샷 라운드 결과에는 영향 없음(best-effort).
    if datetime.now(KST).time() >= _REPORT_REFRESH_AFTER:
        try:
            from ..reports.daily import refresh_snapshot_joins

            saved = refresh_snapshot_joins(today.isoformat(), snapshot_root=out_dir)
            if saved is not None:
                cli.console.print(f"[green]✓ 일일 리포트 조인 재보강:[/green] {saved}")
        except Exception:
            structlog.get_logger(__name__).warning("snapshot.report_refresh_failed", exc_info=True)


@cli.app.command("vi")
def vi(
    market: str = typer.Option(
        "ALL",
        "--market",
        help="시장 구분: ALL / KOSPI / KOSDAQ.",
    ),
) -> None:
    """현재 VI(변동성완화장치) 발동 종목 1회 스냅샷 — 위험신호 로깅용.

    무엇/왜:
    - 키움 `ka10054` 로 지금 VI 가 걸린 종목(급변동으로 거래가 잠시 멈춘 종목)을
      읽어 표로 보여주고 `market.vi.triggered` 구조화 로그를 남긴다. 매매엔 쓰지 않는
      read-only 관찰 도구.
    - 연속 로깅이 필요하면 launchd/cron 으로 이 명령을 주기 호출(예: 장중 5분마다)
      — `snapshot-top200` 과 동일한 운영 패턴.

    어떻게 확인:
    - 장중 1회 실행: `uv run auto-trade vi` → 발동 종목 표 + 건수 출력.
    - 장 마감 후엔 보통 0건(또는 직전 잔여)일 수 있음.
    """
    mkt = market.strip().upper()
    if mkt not in ("ALL", "KOSPI", "KOSDAQ"):
        cli.console.print("[red]--market 는 ALL / KOSPI / KOSDAQ 중 하나여야 합니다.[/red]")
        raise typer.Exit(code=1)

    from ..market.vi import Market, ViStock, vi_triggered

    async def _run() -> list[ViStock]:
        async with cli.KiwoomRestClient() as client:
            return await vi_triggered(client, market=cast(Market, mkt))

    stocks = asyncio.run(_run())
    if not stocks:
        cli.console.print(f"[green]✓ VI 발동 종목 없음[/green] (시장={mkt})")
        return

    table = Table(title=f"VI 발동 종목 ({mkt}) — {len(stocks)}건")
    table.add_column("종목", style="cyan")
    table.add_column("이름")
    table.add_column("유형")
    table.add_column("발동가", justify="right")
    table.add_column("괴리율%", justify="right")
    table.add_column("해제시각")
    table.add_column("발동수", justify="right")
    for s in stocks:
        table.add_row(
            s.symbol,
            s.name,
            s.kind,
            f"{s.trigger_price:,}",
            f"{s.disparity_pct}",
            s.release_time,
            str(s.trigger_count),
        )
    cli.console.print(table)
