"""러너 상태 스냅샷 + 텔레그램 명령 응답 포맷터.

`PaperRunner.state_snapshot()` 가 반환하는 읽기 전용 dataclass 와, 그 dataclass 를
사용자가 텔레그램에서 읽을 한국어 문자열로 변환하는 헬퍼들이 모여있다.

포맷터는 외부 호출 없이 입력 dataclass 만 보고 결정론적으로 문자열을 만든다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
    # 텔레그램 v3 — "running" / "paused" / "stopping" 중 하나.
    # 기본값 "running" 으로 v2 까지의 RunnerStateSnapshot 사용처(테스트 등) 와 호환.
    control_state: str = "running"
    # 21단계 day-trade — "swing" / "day-trade" 중 하나. /status 표시용.
    runner_mode: str = "swing"
    # day-trade 모드일 때만 의미 — "HH:MM" KST. swing 은 빈 문자열.
    liquidate_at_label: str = ""
    # 종목 코드→이름 매핑. 텔레그램 메시지에서 코드 옆에 종목명을 합쳐 표시 (5/14
    # 사용자 피드백). swing 모드나 잔고 없는 신규 시작 시점엔 빈 dict 가 정상.
    symbol_names: dict[str, str] = field(default_factory=dict)
    # 섀도우 세션(게이트 차단일 주문 없는 가상 매매) 여부. 일일 리포트 저장
    # 파일명(shadow-*.json)·텔레그램 라벨 분기용. dry_run 과 별개 — dry_run 은
    # 사람이 수동으로 켜는 검증 모드, shadow 는 차단일 자동 데이터 수집 모드.
    shadow: bool = False


# ---- 포맷터 ----------------------------------------------------------------

HELP_TEXT = (
    "사용 가능한 명령:\n"
    "/status — 러너 가동 상태\n"
    "/balance — 예수금·총평가·종목별 잔고\n"
    "/positions — 보유 종목 요약\n"
    "/pnl — 당일 손익 + 누적 평가손익\n"
    "/last_signal — 종목별 최근 시그널\n"
    "/insight — 직전 7일치 일일 리포트 LLM 분석 (호출당 약 $0.05~0.10, 5분 쿨다운)\n"
    "/pause — 주문 송신만 일시 보류 (시그널은 계속 기록)\n"
    "/resume — /pause 해제, 정상 매매 복귀\n"
    "/unpause — 연속 손절 자동 일시중지의 수동 해제 (BUY 다시 활성화)\n"
    "/stop — 다음 봉에서 graceful 종료 (되돌릴 수 없음)\n"
    "/help — 이 도움말"
)


CONTROL_LABEL_KO: dict[str, str] = {
    "running": "▶ 운영중",
    "paused": "⏸ 일시정지",
    "stopping": "⏹ 종료중",
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


def _with_name(symbol: str, name: str | None) -> str:
    """`005930 삼성전자` 형태 — 종목명 없으면 코드만."""
    if name:
        return f"{symbol} {name}"
    return symbol


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
    control_label = CONTROL_LABEL_KO.get(state.control_state, state.control_state)
    mode_extra = ""
    if state.runner_mode == "day-trade":
        mode_extra = (
            f" · day-trade (청산 {state.liquidate_at_label})"
            if state.liquidate_at_label
            else " · day-trade"
        )
    symbols_label = (
        ", ".join(_with_name(s, state.symbol_names.get(s)) for s in state.symbols)
        if state.symbols
        else "-"
    )
    lines = [
        f"📊 러너 상태{dry}",
        f"제어: {control_label}",
        f"모드: {state.mode}{mode_extra}",
        f"가동: {_uptime(state.started_at, now)}",
        f"종목: {symbols_label}",
        f"전략: {strat_str}",
    ]
    if state.balance_failures > 0:
        lines.append(f"⚠️ 잔고 조회 연속 실패 {state.balance_failures}회")
    return "\n".join(lines)


def format_balance(state: RunnerStateSnapshot) -> str:
    if state.snapshot is None:
        return "잔고 조회 전 — 첫 스냅샷이 도착하면 다시 시도하세요."
    snap = state.snapshot
    # 5/14 회귀 — 키움 mock 잔고 응답이 일시적으로 holdings 빈 채로 옴.
    # 그 시점에 러너 account.positions 가 보유 인지하고 있다면 사용자에게 race 안내.
    if (
        not snap.holdings
        and state.account is not None
        and any(q > 0 for q in state.account.positions.values())
    ):
        return (
            "잔고 응답이 일시적으로 비어 있습니다 — 체결 반영 race로 추정.\n"
            "잠시 후 다시 시도하거나 다음 봉의 자동 갱신을 기다려주세요."
        )
    # 29단계 PR27 — 평가손익 합계는 holdings.pnl 의 로컬 합산을 신뢰 (mock 응답에
    # `tot_evltv_prft` 가 비어 0 으로 들어오는 회귀 차단). 종목별로 ±값이 보이는데
    # 상단 합계가 0 으로 표기되던 5/12 사용자 혼란을 막는다.
    pnl_total = sum((h.pnl for h in snap.holdings), Decimal(0))
    lines = [
        "💰 계좌 잔고",
        f"예수금: {_won(snap.deposit)}",
        f"총평가: {_won(snap.total_evaluation)}",
        f"평가손익: {_signed_won(pnl_total)}",
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
        # 잔고 응답의 h.name 을 먼저 쓰되, 빈 경우 runner._symbol_names 폴백.
        name = h.name if h.name else state.symbol_names.get(h.symbol)
        lines.append(f"• {_with_name(h.symbol, name)} {h.quantity}주 @ 평균 {_won(h.avg_price)}")
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
        sym_label = _with_name(sym, state.symbol_names.get(sym))
        lines.append(
            f"• {sym_label} {sig.side.value.upper()} (w={sig.weight:.2f}){hint}{reason} [{ts}]"
        )
    return "\n".join(lines)
