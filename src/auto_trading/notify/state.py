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


def _pair_buy_liq_records(
    buys: list[dict],
    liqs: list[dict],
) -> tuple[list[tuple[dict, dict]], list[dict], list[dict]]:
    """매수·청산 records → 페어/단방향 분류 (포맷 X, 데이터만).

    같은 종목이 buys 와 liqs 양쪽에 있으면 FIFO 로 raw 페어링 — 매수 3건·청산 2건
    이면 페어 2 + only_buy 1. 5/12 회귀(같은 종목 8회 BUY 인데 첫 1건만 살아남던
    PR27 이전 dedup 동작) 회피. 매수 입력 순서대로 페어 결과를 반환해 호출자가
    안정적인 출력 순서를 가짐.
    """
    liq_queue: dict[str, list[dict]] = {}
    for li in liqs:
        sym = str(li.get("symbol") or "")
        if sym:
            liq_queue.setdefault(sym, []).append(li)

    paired: list[tuple[dict, dict]] = []
    only_buys: list[dict] = []
    for t in buys:
        sym = str(t.get("symbol") or "")
        queue = liq_queue.get(sym)
        if queue:
            paired.append((t, queue.pop(0)))
        else:
            only_buys.append(t)
    only_sells = [li for sym_queue in liq_queue.values() for li in sym_queue]
    return paired, only_buys, only_sells


def _format_pair_line(buy: dict, liq: dict) -> str:
    """`- 005930 삼성전자  10주 @5,000원 → @5,500원  +5,000원/+10.00% (시뮬)`.

    5/14 회귀 — 매수 11주·청산 6주(시장가 청산은 sell_price=None) 흐름에서 매수 수량과
    매수 가격이 그대로 페어 라인에 노출되어 사용자가 "11주가 시장가로 청산됐다" 로 오해.
    페어는 *청산 1건* 단위라 수량은 liq.quantity, 매수가는 liq.buy_price(청산 시점의
    평균가) 를 우선으로 한다.
    """
    sym = str(buy.get("symbol") or liq.get("symbol") or "")
    name_raw = buy.get("name") or liq.get("name")
    head = _with_name(sym, name_raw if isinstance(name_raw, str) else None)
    qty = int(liq.get("quantity") or buy.get("quantity") or 0)
    sim = " (시뮬)" if buy.get("simulated") or liq.get("simulated") else ""
    buy_p = liq.get("buy_price")
    if buy_p is None:
        buy_p = buy.get("price")
    sell_p = liq.get("sell_price")
    buy_part = f"@{_won(buy_p)}" if buy_p is not None else "@시장가"
    sell_part = f"@{_won(sell_p)}" if sell_p is not None else "@시장가"
    pnl_part = ""
    if buy_p is not None and sell_p is not None and float(buy_p) > 0:
        delta = (float(sell_p) - float(buy_p)) * qty
        pct = (float(sell_p) / float(buy_p) - 1.0) * 100.0
        pnl_part = f"  {_signed_won(delta)}/{_signed_pct(pct)}"
    return f"- {head}  {qty}주 {buy_part} → {sell_part}{pnl_part}{sim}"


def _format_buy_line(buy: dict) -> str:
    """매수만 발생 (당일 청산 미진행) — `- 005930 BUY 10주 @5,000원 (시뮬)`."""
    sym = str(buy.get("symbol") or "")
    name = buy.get("name") if isinstance(buy.get("name"), str) else None
    sim = " (시뮬)" if buy.get("simulated") else ""
    qty = int(buy.get("quantity") or 0)
    price = buy.get("price")
    price_part = f" @{_won(price)}" if price is not None else " @시장가"
    return f"- {_with_name(sym, name)} BUY {qty}주{price_part}{sim}"


def _format_sell_line(liq: dict) -> str:
    """청산만 발생 (전일 보유 처분 등) — `- 005930 SELL 10주 @5,500원 (+5,000원/+10.00%) (시뮬)`."""
    sym = str(liq.get("symbol") or "")
    name = liq.get("name") if isinstance(liq.get("name"), str) else None
    sim = " (시뮬)" if liq.get("simulated") else ""
    qty = int(liq.get("quantity") or 0)
    sell = liq.get("sell_price")
    buy = liq.get("buy_price")
    price_part = f" @{_won(sell)}" if sell is not None else " @시장가"
    pnl_part = ""
    if sell is not None and buy is not None and float(buy) > 0:
        delta = (float(sell) - float(buy)) * qty
        pct = (float(sell) / float(buy) - 1.0) * 100.0
        pnl_part = f" ({_signed_won(delta)}/{_signed_pct(pct)})"
    return f"- {_with_name(sym, name)} SELL {qty}주{price_part}{pnl_part}{sim}"


def _build_trade_lines(
    buys: list[dict],
    liqs: list[dict],
) -> tuple[list[str], list[str], list[str]]:
    """매수·청산 records → (페어 라인, 매수만 라인, 청산만 라인) 세 묶음.

    페어링은 `_pair_buy_liq_records`, 라인 포맷은 `_format_*_line` 위임.
    only-buy / only-sell 은 6건까지만 표기 + 초과는 `외 N건` 한 줄.
    """
    paired, only_buys, only_sells = _pair_buy_liq_records(buys, liqs)

    pair_lines = [_format_pair_line(b, li) for b, li in paired]

    only_buy_lines = [_format_buy_line(b) for b in only_buys[:6]]
    if len(only_buys) > 6:
        only_buy_lines.append(f"  외 {len(only_buys) - 6}건")

    only_sell_lines = [_format_sell_line(li) for li in only_sells[:6]]
    if len(only_sells) > 6:
        only_sell_lines.append(f"  외 {len(only_sells) - 6}건")

    return pair_lines, only_buy_lines, only_sell_lines


_TIMELINE_KIND_PREFIX_KO = {
    "discovery": "🔎",
    "buy": "💰",
    "partial_take": "💵",
    "kill_switch": "🛑",
    "liquidate": "🧹",
}


# 29단계 PR27 — truncate 보존 우선순위. 마지막 라운드의 강제청산·손절은 사용자가
# 일일 리포트로 그날의 결말을 파악하는 핵심 신호라 "외 N건" 안쪽에 묻히지 않게 한다.
_TIMELINE_PRIORITY_KINDS: frozenset[str] = frozenset({"kill_switch", "liquidate"})


def _format_timeline_line(ev: dict) -> str:
    kind = str(ev.get("kind") or "")
    time_kst = str(ev.get("time_kst") or "?")
    text = str(ev.get("text") or "")
    if text and text[0] in "🛑💵💰🧹🔎":
        return f"[{time_kst}] {text}"
    prefix = _TIMELINE_KIND_PREFIX_KO.get(kind, "•")
    return f"[{time_kst}] {prefix} {text}"


def _build_timeline_lines(timeline: list, *, max_lines: int = 12) -> list[str]:
    """일일 리포트의 시계열 섹션 — '[09:01] 💵 부분 익절 ...' 한 줄씩.

    payload['timeline'] 은 build_daily_report 가 채운 TimelineEvent asdict 리스트.
    너무 길면 max_lines 까지 자르고 끝에 "외 N건" 안내. 단 kill_switch/liquidate
    이벤트는 truncate 안쪽이라도 항상 포함(5/12 회귀: 13:15·14:51 손절·청산이
    "외 18건" 으로 묻혀 사용자가 그날 결말을 못 봄).
    """
    if not isinstance(timeline, list) or not timeline:
        return []

    head_evs = [ev for ev in timeline[:max_lines] if isinstance(ev, dict)]
    tail = [ev for ev in timeline[max_lines:] if isinstance(ev, dict)]
    preserved = [ev for ev in tail if ev.get("kind") in _TIMELINE_PRIORITY_KINDS]
    dropped = len(tail) - len(preserved)

    out: list[str] = [_format_timeline_line(ev) for ev in head_evs]
    if dropped > 0:
        out.append(f"  외 {dropped}건")
    out.extend(_format_timeline_line(ev) for ev in preserved)
    return out


def _build_closing_balance(payload: dict) -> list[str]:
    """마감 잔고 섹션 — closing_cash 가 있을 때만 렌더. 없으면 빈 리스트.

    2026-05-11 사용자 피드백: '총평가' 라벨이 '예수금 + 보유' 합인지 보유 평가금만인지
    헷갈린다. 라벨을 '보유 평가금' 으로 명확화하고, '계좌 총합' 한 줄을 추가해 합계 표시.
    보유 종목 손익은 '(매수가 대비)' 를 명시.
    """
    cash = payload.get("closing_cash")
    if cash is None:
        return []
    equity = payload.get("closing_equity")
    holdings = payload.get("closing_holdings") or []
    out: list[str] = ["💰 마감 잔고"]
    out.append(f"예수금: {_won(cash)}")
    if equity is not None:
        out.append(f"보유 평가금: {_won(equity)}")
        try:
            total = float(cash) + float(equity)
            out.append(f"계좌 총합: {_won(total)}")
        except TypeError, ValueError:
            pass
    if not holdings:
        out.append("보유: 없음 (전량 청산)")
        return out
    out.append(f"보유: {len(holdings)}종목 (손익은 매수가 대비)")
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


def _daily_discovery_section(payload: dict) -> list[str]:
    """발굴(채택·차단) 섹션. 없으면 빈 리스트."""
    chosen = payload.get("discovery_chosen") or []
    blocked = payload.get("discovery_blocked") or []
    if not (chosen or blocked):
        return []
    chosen_names_raw = payload.get("discovery_chosen_names") or {}
    chosen_names: dict[str, str] = (
        {str(k): str(v) for k, v in chosen_names_raw.items()}
        if isinstance(chosen_names_raw, dict)
        else {}
    )
    head = f"🔎 발굴: 채택 {len(chosen)}건"
    if blocked:
        head += f" · 차단 {len(blocked)}건"
    excluded_etf = int(payload.get("excluded_etf_count") or 0)
    if excluded_etf:
        head += f" (ETF {excluded_etf}건 제외)"
    out = ["", head]
    if chosen:
        out.append(f"선정: {', '.join(_with_name(s, chosen_names.get(s)) for s in chosen)}")
    if blocked:
        shown = ", ".join(f"{_with_name(s, chosen_names.get(s))}({r})" for s, r in blocked[:5])
        tail = f" 외 {len(blocked) - 5}" if len(blocked) > 5 else ""
        out.append(f"차단: {shown}{tail}")
    return out


def _daily_gates_section(payload: dict) -> list[str]:
    """시장 게이트 섹션. 없으면 빈 리스트."""
    gates = payload.get("market_gates") or []
    if not gates:
        return []
    out = ["", "📈 시장 게이트"]
    for g in gates:
        mark = "✅" if g.get("above") else "❌"
        last = g.get("last")
        sma5 = g.get("sma5")
        sma20 = g.get("sma20")
        metric = ""
        if last is not None and sma5 is not None and sma20 is not None:
            metric = f" (last {float(last):.2f} / 5 {float(sma5):.2f} / 20 {float(sma20):.2f})"
        out.append(f"- {g.get('market', '?')} {mark}{metric}")
    return out


def _daily_timeline_section(payload: dict) -> list[str]:
    """오늘 흐름(타임라인) 섹션. 없으면 빈 리스트."""
    timeline_lines = _build_timeline_lines(payload.get("timeline") or [])
    if not timeline_lines:
        return []
    return ["", f"⏱ 오늘 흐름 ({len(timeline_lines)}건)", *timeline_lines]


def _daily_trades_section(buys: list, liqs: list) -> list[str]:
    """매매 결과/매수/청산 섹션. 빈 하위 섹션은 생략."""
    pair_lines, only_buy_lines, only_sell_lines = _build_trade_lines(buys, liqs)
    out: list[str] = []
    if pair_lines:
        out += ["", f"📋 매매 결과: {len(pair_lines)}건", *pair_lines]
    if only_buy_lines:
        out += ["", f"💰 매수: {len(only_buy_lines)}건", *only_buy_lines]
    if only_sell_lines:
        out += ["", f"🧹 청산: {len(only_sell_lines)}건", *only_sell_lines]
    return out


def _daily_summary_section(payload: dict, buys: list, liqs: list) -> list[str]:
    """요약(실현손익·거래대금·회전·에러) + 비용 차감 PnL 섹션."""
    realized = payload.get("realized_pnl")
    realized_pct = payload.get("realized_pnl_pct")
    realized_after_cost = payload.get("realized_pnl_after_cost")
    err = int(payload.get("error_count") or 0)
    summary_parts: list[str] = []
    if realized is not None:
        pct_part = f"/{_signed_pct(realized_pct)}" if realized_pct is not None else ""
        summary_parts.append(f"실현 {_signed_won(realized)}{pct_part}")
    # 2026-05-11 사용자 피드백: "매매 N건" 이 매수·청산 합산이라 헷갈림 → 각각 표기.
    summary_parts.append(f"매수 {len(buys)}건 · 청산 {len(liqs)}건")
    # 과매매 가시화 — 손실 핵심 원인이 장중 과회전. 거래대금·회전율을 요약에 노출.
    traded_notional = payload.get("traded_notional")
    if traded_notional is not None and (buys or liqs):
        summary_parts.append(f"거래대금 {_won(traded_notional)}")
    turnover = payload.get("turnover_ratio")
    if turnover is not None:
        summary_parts.append(f"회전 {float(turnover):.1f}배")
    if err:
        # 통짜 "에러 N건" 은 진단 불가 → 유형별 분해(0 인 항목은 생략).
        o = int(payload.get("orders_failed_count") or 0)
        liq_f = int(payload.get("liquidations_failed_count") or 0)
        bal = int(payload.get("balance_failure_count") or 0)
        fat = int(payload.get("fatal_count") or 0)
        breakdown = [
            label
            for cnt, label in (
                (o, f"주문 {o}"),
                (liq_f, f"청산 {liq_f}"),
                (bal, f"잔고 {bal}"),
                (fat, f"치명 {fat}"),
            )
            if cnt
        ]
        summary_parts.append("에러 " + " · ".join(breakdown) if breakdown else f"에러 {err}건")
    out: list[str] = []
    if summary_parts:
        # 📋 는 매매 결과 섹션이 쓰므로 요약은 다른 이모지로 시각 구분.
        out += ["", "📊 " + " · ".join(summary_parts)]
    # 비용 차감 PnL — 23단계 Phase A. 비용이 0이라 값이 같아도 초보에게
    # "수수료·세금·슬리피지 반영됨" 을 명시하려 거래가 있으면 항상 함께 노출.
    if realized is not None and realized_after_cost is not None:
        out.append(f"💸 비용 차감 PnL: {_signed_won(realized_after_cost)}")
    return out


def format_daily_report(payload: dict) -> str:
    """22단계 Phase A — 일일 리포트 텔레그램 메시지.

    payload 는 `reports.daily.DailyReport` 의 asdict 결과(+tuple→list 변환).
    """
    date = str(payload.get("date") or "?")
    mode = str(payload.get("mode") or "?")
    dry = " · dry-run" if payload.get("dry_run") else ""
    lines = [f"📊 일일 리포트 ({date})", f"모드: {mode}{dry}"]

    buys = payload.get("buys") or []
    liqs = payload.get("liquidations") or []

    lines += _daily_discovery_section(payload)
    lines += _daily_gates_section(payload)
    lines += _daily_timeline_section(payload)
    lines += _daily_trades_section(buys, liqs)

    closing_lines = _build_closing_balance(payload)
    if closing_lines:
        lines.append("")
        lines.extend(closing_lines)

    lines += _daily_summary_section(payload, buys, liqs)

    note = str(payload.get("note_ko") or "")
    if note:
        lines.append(f"메모: {note}")

    return "\n".join(lines)
