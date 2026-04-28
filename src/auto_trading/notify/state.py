"""러너 상태 스냅샷 + 텔레그램 명령 응답 포맷터.

`PaperRunner.state_snapshot()` 가 반환하는 읽기 전용 dataclass 와, 그 dataclass 를
사용자가 텔레그램에서 읽을 한국어 문자열로 변환하는 헬퍼들이 모여있다.

포맷터는 외부 호출 없이 입력 dataclass 만 보고 결정론적으로 문자열을 만든다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ..account.balance import AccountSnapshot
from ..risk.manager import AccountState
from ..strategy.base import Signal


@dataclass(frozen=True)
class RunnerStateSnapshot:
    """러너 인메모리 상태의 읽기 전용 사본."""

    mode: str  # "mock" / "real"
    dry_run: bool
    symbols: list[str]
    strategies: dict[str, str]  # {symbol: strategy_class_name}
    started_at: datetime | None
    snapshot: AccountSnapshot | None  # 키움 raw 잔고
    account: AccountState | None  # 리스크 평가용 derived
    recent_signals: dict[str, Signal]  # 종목별 최신 1건
    balance_failures: int
    state: str = "running"  # RunnerState enum value: "running" | "paused" | "stopping"


@dataclass(frozen=True)
class RunnerControl:
    """텔레그램 v3 제어 명령 — 핸들러가 러너를 직접 알지 않게 콜러블만 묶어 주입한다."""

    pause: Callable[[], None]
    resume: Callable[[], None]
    request_stop: Callable[[], None]


# ---- 포맷터 ----------------------------------------------------------------

HELP_TEXT = (
    "사용 가능한 명령:\n"
    "/status — 러너 가동 상태\n"
    "/balance — 예수금·총평가·종목별 잔고\n"
    "/positions — 보유 종목 요약\n"
    "/pnl — 당일 손익 + 누적 평가손익\n"
    "/last_signal — 종목별 최근 시그널\n"
    "/pause — 신호·주문 일시정지 (WS 연결은 유지)\n"
    "/resume — /pause 해제\n"
    "/stop — 러너 종료\n"
    "/help — 이 도움말"
)


_STATE_LABEL = {
    "running": "🟢 가동중",
    "paused": "⏸ 일시정지",
    "stopping": "⏹ 정지중",
}


def _won(value: Decimal | float | int) -> str:
    """천단위 콤마 + 원."""
    return f"{int(Decimal(str(value))):,}원"


def _signed_won(value: Decimal | float | int) -> str:
    n = int(Decimal(str(value)))
    sign = "+" if n > 0 else ""
    return f"{sign}{n:,}원"


def _signed_pct(value: Decimal | float | int) -> str:
    n = float(Decimal(str(value)))
    sign = "+" if n > 0 else ""
    return f"{sign}{n:.2f}%"


def _uptime(started_at: datetime | None, now: datetime) -> str:
    if started_at is None:
        return "-"
    sec = int((now - started_at).total_seconds())
    if sec < 60:
        return f"{sec}초"
    if sec < 3600:
        return f"{sec // 60}분 {sec % 60}초"
    h, rem = divmod(sec, 3600)
    return f"{h}시간 {rem // 60}분"


def format_status(state: RunnerStateSnapshot, *, now: datetime | None = None) -> str:
    now = now or datetime.now()
    dry = " (dry-run)" if state.dry_run else ""
    strat_str = (
        ", ".join(f"{s}={state.strategies[s]}" for s in state.symbols) if state.strategies else "-"
    )
    lines = [
        f"📊 러너 상태{dry}",
        f"상태: {_STATE_LABEL.get(state.state, state.state)}",
        f"모드: {state.mode}",
        f"가동: {_uptime(state.started_at, now)}",
        f"종목: {', '.join(state.symbols) if state.symbols else '-'}",
        f"전략: {strat_str}",
    ]
    if state.balance_failures > 0:
        lines.append(f"⚠️ 잔고 조회 연속 실패 {state.balance_failures}회")
    return "\n".join(lines)


def format_balance(state: RunnerStateSnapshot) -> str:
    if state.snapshot is None:
        return "잔고 조회 전 — 첫 스냅샷이 도착하면 다시 시도하세요."
    snap = state.snapshot
    lines = [
        "💰 계좌 잔고",
        f"예수금: {_won(snap.deposit)}",
        f"총평가: {_won(snap.total_evaluation)}",
        f"평가손익: {_signed_won(snap.total_pnl)}",
    ]
    if snap.holdings:
        lines.append("")
        lines.append("보유 종목:")
        for h in snap.holdings:
            lines.append(
                f"• {h.name or h.symbol} ({h.symbol}) {h.quantity}주"
                f" / 평균 {_won(h.avg_price)}"
                f" / 평가 {_won(h.market_value)}"
                f" / 손익 {_signed_won(h.pnl)} ({_signed_pct(h.pnl_pct)})"
            )
    else:
        lines.append("보유 종목: 없음")
    return "\n".join(lines)


def format_positions(state: RunnerStateSnapshot) -> str:
    if state.snapshot is None or not state.snapshot.holdings:
        return "포지션 없음"
    lines = ["📦 보유 포지션"]
    for h in state.snapshot.holdings:
        lines.append(f"• {h.symbol} {h.quantity}주 @ 평균 {_won(h.avg_price)}")
    return "\n".join(lines)


def format_pnl(state: RunnerStateSnapshot) -> str:
    lines = ["📈 손익"]
    if state.account is not None and state.account.day_start_equity > 0:
        delta = state.account.total_equity - state.account.day_start_equity
        pct = delta / state.account.day_start_equity * 100.0
        lines.append(f"당일: {_signed_won(delta)} ({_signed_pct(pct)})")
    else:
        lines.append("당일: -")
    if state.snapshot is not None:
        lines.append(f"누적 평가손익: {_signed_won(state.snapshot.total_pnl)}")
    else:
        lines.append("누적 평가손익: -")
    return "\n".join(lines)


def format_last_signal(state: RunnerStateSnapshot) -> str:
    if not state.recent_signals:
        return "아직 시그널 없음"
    lines = ["🔔 최근 시그널 (종목별 1건)"]
    for sym in sorted(state.recent_signals):
        sig = state.recent_signals[sym]
        ts = sig.ts.strftime("%H:%M") if sig.ts else "-"
        hint = f" @ {_won(sig.price_hint)}" if sig.price_hint is not None else ""
        reason = f" — {sig.reason}" if sig.reason else ""
        lines.append(f"• {sym} {sig.side.value.upper()} (w={sig.weight:.2f}){hint}{reason} [{ts}]")
    return "\n".join(lines)
