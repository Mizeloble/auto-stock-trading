"""러너 상태 스냅샷 + 텔레그램 명령 응답 포맷터.

`PaperRunner.state_snapshot()` 가 반환하는 읽기 전용 dataclass 와, 그 dataclass 를
사용자가 텔레그램에서 읽을 한국어 문자열로 변환하는 헬퍼들이 모여있다.

포맷터는 외부 호출 없이 입력 dataclass 만 보고 결정론적으로 문자열을 만든다.
"""

from __future__ import annotations

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
    # 텔레그램 v3 — "running" / "paused" / "stopping" 중 하나.
    # 기본값 "running" 으로 v2 까지의 RunnerStateSnapshot 사용처(테스트 등) 와 호환.
    control_state: str = "running"
    # 21단계 day-trade — "swing" / "day-trade" 중 하나. /status 표시용.
    runner_mode: str = "swing"
    # day-trade 모드일 때만 의미 — "HH:MM" KST. swing 은 빈 문자열.
    liquidate_at_label: str = ""


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


def _build_trade_lines(
    buys: list[dict],
    liqs: list[dict],
) -> tuple[list[str], list[str], list[str]]:
    """매수·청산 records → (페어 라인, 매수만 라인, 청산만 라인) 세 묶음.

    같은 종목이 buys 와 liqs 양쪽에 있으면 페어로, 한쪽만 있으면 별 묶음으로.
    여러 건 매수가 한 종목에 누적되면 첫 매수만 페어 베이스로 본다 — day-trade 1:1 가정.
    """
    buy_by_sym: dict[str, dict] = {}
    for t in buys:
        sym = str(t.get("symbol") or "")
        if sym and sym not in buy_by_sym:
            buy_by_sym[sym] = t
    liq_by_sym: dict[str, dict] = {}
    for li in liqs:
        sym = str(li.get("symbol") or "")
        if sym and sym not in liq_by_sym:
            liq_by_sym[sym] = li

    paired_syms = [s for s in buy_by_sym if s in liq_by_sym]
    only_buy_syms = [s for s in buy_by_sym if s not in liq_by_sym]
    only_sell_syms = [s for s in liq_by_sym if s not in buy_by_sym]

    pair_lines: list[str] = []
    for sym in paired_syms:
        b = buy_by_sym[sym]
        li = liq_by_sym[sym]
        name = b.get("name") or li.get("name")
        head = _with_name(sym, name if isinstance(name, str) else None)
        qty = int(b.get("quantity") or 0)
        sim = " (시뮬)" if b.get("simulated") or li.get("simulated") else ""
        buy_p = b.get("price")
        sell_p = li.get("sell_price")
        if buy_p is None:
            buy_p = li.get("buy_price")
        buy_part = f"@{_won(buy_p)}" if buy_p is not None else "@시장가"
        sell_part = f"@{_won(sell_p)}" if sell_p is not None else "@시장가"
        pnl_part = ""
        if buy_p is not None and sell_p is not None and float(buy_p) > 0:
            delta = (float(sell_p) - float(buy_p)) * qty
            pct = (float(sell_p) / float(buy_p) - 1.0) * 100.0
            pnl_part = f"  {_signed_won(delta)}/{_signed_pct(pct)}"
        pair_lines.append(f"- {head}  {qty}주 {buy_part} → {sell_part}{pnl_part}{sim}")

    only_buy_lines: list[str] = []
    for sym in only_buy_syms[:6]:
        t = buy_by_sym[sym]
        name = t.get("name") if isinstance(t.get("name"), str) else None
        sim = " (시뮬)" if t.get("simulated") else ""
        qty = int(t.get("quantity") or 0)
        price = t.get("price")
        price_part = f" @{_won(price)}" if price is not None else " @시장가"
        only_buy_lines.append(f"- {_with_name(sym, name)} BUY {qty}주{price_part}{sim}")
    if len(only_buy_syms) > 6:
        only_buy_lines.append(f"  외 {len(only_buy_syms) - 6}건")

    only_sell_lines: list[str] = []
    for sym in only_sell_syms[:6]:
        li = liq_by_sym[sym]
        name = li.get("name") if isinstance(li.get("name"), str) else None
        sim = " (시뮬)" if li.get("simulated") else ""
        qty = int(li.get("quantity") or 0)
        sell = li.get("sell_price")
        buy = li.get("buy_price")
        price_part = f" @{_won(sell)}" if sell is not None else " @시장가"
        pnl_part = ""
        if sell is not None and buy is not None and float(buy) > 0:
            delta = (float(sell) - float(buy)) * qty
            pct = (float(sell) / float(buy) - 1.0) * 100.0
            pnl_part = f" ({_signed_won(delta)}/{_signed_pct(pct)})"
        only_sell_lines.append(f"- {_with_name(sym, name)} SELL {qty}주{price_part}{pnl_part}{sim}")
    if len(only_sell_syms) > 6:
        only_sell_lines.append(f"  외 {len(only_sell_syms) - 6}건")

    return pair_lines, only_buy_lines, only_sell_lines


def _build_closing_balance(payload: dict) -> list[str]:
    """마감 잔고 섹션 — closing_cash 가 있을 때만 렌더. 없으면 빈 리스트."""
    cash = payload.get("closing_cash")
    if cash is None:
        return []
    equity = payload.get("closing_equity")
    holdings = payload.get("closing_holdings") or []
    out: list[str] = ["💰 마감 잔고"]
    out.append(f"예수금: {_won(cash)}")
    if equity is not None:
        out.append(f"총평가: {_won(equity)}")
    if not holdings:
        out.append("보유: 없음 (전량 청산)")
        return out
    out.append(f"보유: {len(holdings)}종목")
    for h in holdings[:6]:
        sym = str(h.get("symbol") or "?")
        name = h.get("name") if isinstance(h.get("name"), str) else None
        qty = int(h.get("quantity") or 0)
        cur = h.get("current_price")
        pnl = h.get("pnl")
        pct = h.get("pnl_pct")
        cur_part = f" @{_won(cur)}" if cur is not None else ""
        pnl_part = ""
        if pnl is not None and pct is not None:
            pnl_part = f" ({_signed_won(pnl)}/{_signed_pct(pct)})"
        out.append(f"- {_with_name(sym, name)} {qty}주{cur_part}{pnl_part}")
    if len(holdings) > 6:
        out.append(f"  외 {len(holdings) - 6}종목")
    return out


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
    lines = [
        f"📊 러너 상태{dry}",
        f"제어: {control_label}",
        f"모드: {state.mode}{mode_extra}",
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


def format_daily_report(payload: dict) -> str:
    """22단계 Phase A — 일일 리포트 텔레그램 메시지.

    payload 는 `reports.daily.DailyReport` 의 asdict 결과(+tuple→list 변환).
    """
    date = str(payload.get("date") or "?")
    mode = str(payload.get("mode") or "?")
    dry = " · dry-run" if payload.get("dry_run") else ""
    lines = [f"📊 일일 리포트 ({date})", f"모드: {mode}{dry}"]

    chosen = payload.get("discovery_chosen") or []
    blocked = payload.get("discovery_blocked") or []
    chosen_names_raw = payload.get("discovery_chosen_names") or {}
    chosen_names: dict[str, str] = (
        {str(k): str(v) for k, v in chosen_names_raw.items()}
        if isinstance(chosen_names_raw, dict)
        else {}
    )
    if chosen or blocked:
        lines.append("")
        head = f"🔎 발굴: 채택 {len(chosen)}건"
        if blocked:
            head += f" · 차단 {len(blocked)}건"
        excluded_etf = int(payload.get("excluded_etf_count") or 0)
        if excluded_etf:
            head += f" (ETF {excluded_etf}건 제외)"
        lines.append(head)
        if chosen:
            lines.append(f"선정: {', '.join(_with_name(s, chosen_names.get(s)) for s in chosen)}")
        if blocked:
            shown = ", ".join(f"{s}({r})" for s, r in blocked[:5])
            tail = f" 외 {len(blocked) - 5}" if len(blocked) > 5 else ""
            lines.append(f"차단: {shown}{tail}")

    gates = payload.get("market_gates") or []
    if gates:
        lines.append("")
        lines.append("📈 시장 게이트")
        for g in gates:
            mark = "✅" if g.get("above") else "❌"
            last = g.get("last")
            sma5 = g.get("sma5")
            sma20 = g.get("sma20")
            metric = ""
            if last is not None and sma5 is not None and sma20 is not None:
                metric = f" (last {float(last):.2f} / 5 {float(sma5):.2f} / 20 {float(sma20):.2f})"
            lines.append(f"- {g.get('market', '?')} {mark}{metric}")

    buys = payload.get("buys") or []
    liqs = payload.get("liquidations") or []
    pair_lines, only_buy_lines, only_sell_lines = _build_trade_lines(buys, liqs)
    if pair_lines:
        lines.append("")
        lines.append(f"📋 매매 결과: {len(pair_lines)}건")
        lines.extend(pair_lines)
    if only_buy_lines:
        lines.append("")
        lines.append(f"💰 매수: {len(only_buy_lines)}건")
        lines.extend(only_buy_lines)
    if only_sell_lines:
        lines.append("")
        lines.append(f"🧹 청산: {len(only_sell_lines)}건")
        lines.extend(only_sell_lines)

    closing_lines = _build_closing_balance(payload)
    if closing_lines:
        lines.append("")
        lines.extend(closing_lines)

    realized = payload.get("realized_pnl")
    realized_pct = payload.get("realized_pnl_pct")
    realized_after_cost = payload.get("realized_pnl_after_cost")
    err = int(payload.get("error_count") or 0)
    summary_parts: list[str] = []
    if realized is not None:
        pct_part = f"/{_signed_pct(realized_pct)}" if realized_pct is not None else ""
        summary_parts.append(f"실현 {_signed_won(realized)}{pct_part}")
    summary_parts.append(f"매매 {len(buys) + len(liqs)}건")
    if err:
        summary_parts.append(f"에러 {err}건")
    if summary_parts:
        lines.append("")
        # 📋 는 매매 결과 섹션이 쓰므로 요약은 다른 이모지로 시각 구분.
        lines.append("📊 " + " · ".join(summary_parts))

    # 비용 차감 PnL — 23단계 Phase A. 같은 값이면 노이즈라 생략.
    if (
        realized is not None
        and realized_after_cost is not None
        and float(realized_after_cost) != float(realized)
    ):
        lines.append(f"💸 비용 차감 PnL: {_signed_won(realized_after_cost)}")

    note = str(payload.get("note_ko") or "")
    if note:
        lines.append(f"메모: {note}")

    return "\n".join(lines)
