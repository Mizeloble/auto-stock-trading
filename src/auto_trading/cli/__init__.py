"""`auto-trade` CLI 엔트리 (패키지).

명령은 도메인별 서브모듈로 분리되어 있다:
- `_trading`   — quote / balance / verify-symbol / paper / paper-day
- `_backtest`  — backtest / backtest-compare (walk-forward · CPCV 포함)
- `_strategy`  — strategy advise / suggest
- `_reporting` — report / insight / weekly-proposal / fetch-news / detect-regime
- `_ops`       — dashboard / heartbeat-check / preflight / latency / data / orders

이 `__init__` 은 `app`·서브 Typer·공용 헬퍼·외부 의존(테스트가 `auto_trading.cli.<name>`
으로 패치하는 심볼)을 한 네임스페이스에 모아 둔다. 서브모듈은 `from auto_trading import
cli` 후 `cli.<name>` 으로 참조하므로 기존 monkeypatch 시맨틱이 그대로 유지된다.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, time

import structlog
import typer
from rich.console import Console

from .._common import KST
from ..config import get_settings
from ..daytrade.cli_helpers import (
    apply_daytrade_profile_overrides,
    build_paper_day_runner,
    paper_day_main,
    paper_day_with_commands,
)
from ..kiwoom.rest import KiwoomRestClient
from ..market import calendar as krx_calendar
from ..market.chart import bars_to_dataframe, get_daily_bars
from ..market.quote import get_quote
from ..notify.factory import build_command_handler, build_notifier

app = typer.Typer(help="키움 자동매매 봇 CLI (모의투자 기본)")
strategy_app = typer.Typer(help="전략 추천·프로파일 관리")
app.add_typer(strategy_app, name="strategy")
data_app = typer.Typer(help="데이터 파일 진단·정합성 검사.")
app.add_typer(data_app, name="data")
orders_app = typer.Typer(help="주문 저널 진단·복구 도구.")
app.add_typer(orders_app, name="orders")
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


def _parse_cpcv_spec(spec: str) -> tuple[int, int, int]:
    """\"10\" / \"10/2\" / \"10/2/3\" → (n_folds, embargo_bars, n_test_folds).

    - 'N'       → (N, 0, 1)   단순 purged k-fold (PR37, Phase 1)
    - 'N/E'     → (N, E, 1)   embargo 추가
    - 'N/E/k'   → (N, E, k)   combinatorial (PR38, Phase 2) — C(N,k) 조합

    29단계 PR37 (Phase 1) + PR44 (combinatorial CLI).
    """
    parts = spec.split("/")
    if len(parts) not in (1, 2, 3):
        raise typer.BadParameter(
            "--cpcv 는 'N' / 'N/E' / 'N/E/k' 형식 (예: '10', '10/2', '10/2/3')."
        )
    try:
        n_folds = int(parts[0])
        embargo = int(parts[1]) if len(parts) >= 2 else 0
        n_test_folds = int(parts[2]) if len(parts) == 3 else 1
    except ValueError as exc:
        raise typer.BadParameter(f"숫자 파싱 실패: {spec}") from exc
    if n_folds < 2:
        raise typer.BadParameter("--cpcv N 은 2 이상이어야 합니다.")
    if embargo < 0:
        raise typer.BadParameter("--cpcv embargo 는 음수 불가.")
    if not (1 <= n_test_folds <= n_folds):
        raise typer.BadParameter(f"--cpcv k 는 1..N 범위여야 합니다 (N={n_folds}).")
    return n_folds, embargo, n_test_folds


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


def _parse_hhmm_kst(label: str) -> time:
    """'15:15' 같은 문자열을 KST time 으로. 잘못된 형식이면 BadParameter."""
    try:
        h, m = label.split(":")
        return time(int(h), int(m), tzinfo=KST)
    except (ValueError, AttributeError) as exc:
        raise typer.BadParameter(
            f"--liquidate 는 'HH:MM' (예: 15:15) 형식이어야 합니다: {label!r}"
        ) from exc


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


# 서브모듈 import 가 데코레이터를 통해 app/strategy_app/data_app/orders_app 에
# 명령을 등록한다. app·헬퍼·외부 의존이 모두 정의된 뒤에 와야 한다.
from . import _backtest, _ops, _reporting, _strategy, _trading  # noqa: E402,F401

__all__ = [
    "KiwoomRestClient",
    "_build_anthropic_client",
    "_coerce_param",
    "_fetch_daily_bars",
    "_holiday_guard",
    "_parse_cpcv_spec",
    "_parse_hhmm_kst",
    "_parse_symbol_spec",
    "_parse_walk_forward_spec",
    "_root",
    "_setup_logging",
    "app",
    "apply_daytrade_profile_overrides",
    "bars_to_dataframe",
    "build_command_handler",
    "build_notifier",
    "build_paper_day_runner",
    "console",
    "data_app",
    "get_daily_bars",
    "get_quote",
    "get_settings",
    "krx_calendar",
    "orders_app",
    "paper_day_main",
    "paper_day_with_commands",
    "strategy_app",
]


if __name__ == "__main__":
    app()
