"""DailyReport 도메인 + 로그·스냅샷 → 리포트 빌더 + JSON I/O.

빌더는 두 입력만 사용:
1. `LogEvents` (그날 로그 파일 파싱 결과)
2. `RunnerStateSnapshot` (러너의 인메모리 상태) — 선택적, 종료 직후만 가용

LLM 호출·외부 API 호출 없음. 결정론적 — 같은 입력이면 같은 출력.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any

from ..notify.state import RunnerStateSnapshot
from .log_parser import LogEvents

# 비용 추정 상수 — backtest/broker.py 의 기본값과 일치시킨다 (단일 진실 출처).
# 두 모듈을 import 로 묶지 않는 이유: reports/ 는 외부 호출 금지(CLAUDE.md), 의존 최소화.
# broker.py 가 변경되면 여기 값도 같이 갱신할 것.
COST_COMMISSION_RATE = 0.00015  # 0.015% 매수+매도
COST_TAX_RATE_SELL = 0.0020  # 0.20% 매도 시 (2026 인상)
COST_SLIPPAGE_BPS = 5.0  # 0.05% 매수+매도


@dataclass(frozen=True)
class TradeRecord:
    """매수·매도 1건. price 는 로그에 남아있을 때만(시장가는 None)."""

    symbol: str
    side: str  # "buy" / "sell"
    quantity: int
    price: float | None
    simulated: bool  # dry-run 시뮬이면 True
    name: str | None = None  # 종목명 — 발굴 로그에서 알 수 있을 때만 채움


@dataclass(frozen=True)
class LiquidationRecord:
    """청산 1건. 매수 가격까지 알면 buy_price 채움 → pnl 계산."""

    symbol: str
    quantity: int
    sell_price: float | None
    buy_price: float | None
    simulated: bool
    name: str | None = None

    @property
    def pnl(self) -> float | None:
        if self.sell_price is None or self.buy_price is None:
            return None
        return (self.sell_price - self.buy_price) * self.quantity

    @property
    def pnl_pct(self) -> float | None:
        if self.sell_price is None or self.buy_price is None or self.buy_price == 0:
            return None
        return (self.sell_price / self.buy_price - 1.0) * 100.0


@dataclass(frozen=True)
class MarketGateInfo:
    """시장 게이트 1건 (KOSPI / KOSDAQ)."""

    market: str
    last: float | None
    sma5: float | None
    sma20: float | None
    above: bool


@dataclass(frozen=True)
class TimelineEvent:
    """일일 리포트의 시계열 한 줄.

    2026-05-11 회귀 후속 — 사용자가 "오늘 09:01 무슨 일이 일어났지?" 를 즉시 파악할 수
    있게 발굴·매수·부분 익절·kill switch·청산 시작을 시각 순으로 한 줄씩 합쳐서 보여줌.
    text 는 이미 사람이 읽는 형식으로 빌더가 채움 — 포매터는 시각 + text 만 출력.
    """

    time_kst: str  # "HH:MM" KST. 로그가 UTC ISO 8601 라 빌더가 변환.
    kind: str  # "discovery" | "buy" | "partial_take" | "kill_switch" | "liquidate"
    text: str  # 한국어 한 줄 — 종목·수량·가격·비율 등 포함


@dataclass(frozen=True)
class ClosingHolding:
    """마감 시점 보유 종목 1건. 러너 종료 직후 키움 잔고 스냅샷에서 추출."""

    symbol: str
    name: str
    quantity: int
    avg_price: float
    current_price: float
    market_value: float
    pnl: float
    pnl_pct: float


@dataclass(frozen=True)
class DailyReport:
    """하루치 트레이딩 요약. JSON 직렬화 → `data/reports/daily-YYYY-MM-DD.json`."""

    date: str  # YYYY-MM-DD
    mode: str  # "day-trade" / "swing"
    dry_run: bool
    discovery_chosen: list[str]
    discovery_blocked: list[tuple[str, str]]
    market_gates: list[MarketGateInfo]
    buys: list[TradeRecord]
    liquidations: list[LiquidationRecord]
    error_count: int
    note_ko: str
    # 메타. 누적 손익(가능한 경우만 채워진다 — 시장가 매수/매도 가격이 둘 다 있을 때).
    realized_pnl: float | None = None
    realized_pnl_pct: float | None = None
    # 장중 청산(stop-loss / trailing / partial-take) SELL 체결. EOD 강제청산은
    # liquidations 가 따로 담는다 — 두 경로의 이벤트명이 달라 중복 집계되지 않는다.
    # 2026-05-16: 이전엔 side=="buy" 만 추출하고 SELL 을 통째로 버려 realized_pnl 이
    # 항상 None 이었음(주간 인사이트가 "성과 검증 불가" 로 오판). 이 필드로 복구.
    sells: list[TradeRecord] = field(default_factory=list)
    excluded_etf_count: int = 0
    rank_candidates: list[str] = field(default_factory=list)
    # 코드→종목명. discovery 로그의 names 필드에서 흡수. 없는 종목은 키 자체가 없음.
    discovery_chosen_names: dict[str, str] = field(default_factory=dict)
    # 마감 시점 잔고 — 러너 인메모리 snapshot 가용 시만 채움 (자동 호출 시 ✓, 수동 재생성 X).
    # day-trade 청산 후엔 closing_holdings 가 빈 리스트인 게 정상.
    closing_cash: int | None = None
    closing_equity: int | None = None
    closing_total_pnl: int | None = None
    closing_holdings: list[ClosingHolding] = field(default_factory=list)
    # 23단계 Phase A · 비용 추정 — mock/시뮬은 키움이 비용 0 으로 응답하므로 daily.py 에서 계산해 주입.
    # 실거래는 키움 응답에 비용이 들어있지만, 일관성·교차 검증 차원에서 동일 추정도 함께 저장.
    total_fees: float | None = None  # 매수+매도 수수료 합 (COST_COMMISSION_RATE * 2)
    total_taxes: float | None = None  # 매도 거래세 합 (COST_TAX_RATE_SELL)
    total_slippage: float | None = None  # 매수+매도 슬리피지 합 (COST_SLIPPAGE_BPS * 2)
    realized_pnl_after_cost: float | None = None  # realized_pnl - (fees + taxes + slippage)
    # 2026-05-11 회귀 후속 — 시계열 한 줄 요약. 로그에 timestamp 가 없거나 파싱 불가면 빈 리스트.
    timeline: list[TimelineEvent] = field(default_factory=list)


# ---- 빌더 ---------------------------------------------------------------


def build_daily_report(
    *,
    date: str,
    events: LogEvents,
    snapshot: RunnerStateSnapshot | None,
) -> DailyReport:
    """LogEvents + 옵셔널 snapshot 으로 DailyReport 생성.

    무엇/왜:
    - 자동 호출(러너 종료 직후) 은 snapshot 이 있어 mode/dry_run 을 알 수 있음.
    - 수동 재생성(`auto-trade report --date …`) 은 snapshot=None — 로그만 보고 추정.
    - day-trade 가 아닌 일자(레거시 swing 또는 데이터 없음) 도 빈 DailyReport 로 안전하게 만듦.
    """
    discovery_chosen, discovery_blocked = _extract_discovery(events)
    market_gates = _extract_gates(events)
    names = dict(events.symbol_names)
    buys, sells, liquidations = _extract_trades(events, names)
    error_count = (
        len(events.orders_failed)
        + len(events.liquidations_failed)
        + len(events.fatal)
        + (1 if events.balance_failures > 0 else 0)
    )

    # snapshot 우선. 없으면 events 로 mode/dry_run 추정.
    if snapshot is not None:
        mode = snapshot.runner_mode
        dry_run = snapshot.dry_run
    else:
        mode = "day-trade" if (events.discovery is not None or events.rank is not None) else "swing"
        dry_run = bool(events.orders_simulated or events.liquidations_simulated)

    realized_pnl, realized_pnl_pct = _aggregate_realized(buys, sells, liquidations)
    closing_cash, closing_equity, closing_total_pnl, closing_holdings = _extract_closing(snapshot)
    # snapshot.holdings 의 name 도 흡수 — discovery 에 없던 종목(swing 등) 보강.
    if snapshot is not None and snapshot.snapshot is not None:
        for h in snapshot.snapshot.holdings:
            if h.name and h.symbol not in names:
                names[h.symbol] = h.name
    chosen_names = {sym: names[sym] for sym in discovery_chosen if sym in names}
    total_fees, total_taxes, total_slippage = _estimate_costs(buys, sells, liquidations)
    realized_pnl_after_cost = (
        realized_pnl - (total_fees + total_taxes + total_slippage)
        if realized_pnl is not None
        and total_fees is not None
        and total_taxes is not None
        and total_slippage is not None
        else None
    )

    note_ko = _summary_ko(
        chosen=discovery_chosen,
        buys=buys,
        sells=sells,
        blocked=discovery_blocked,
        liquidations=liquidations,
        realized_pnl=realized_pnl,
        realized_pnl_after_cost=realized_pnl_after_cost,
        error_count=error_count,
    )

    timeline = _build_timeline(events, names)

    rank_candidates: list[str] = []
    excluded = 0
    if events.rank is not None:
        chosen = events.rank.get("chosen") or []
        # chosen 항목은 [symbol, market, volume] 또는 단순 symbol 리스트일 수 있음 — 양쪽 처리.
        for item in chosen:
            if isinstance(item, list | tuple) and item:
                rank_candidates.append(str(item[0]))
            elif isinstance(item, str):
                rank_candidates.append(item)
        excluded_raw = events.rank.get("excluded_count")
        if isinstance(excluded_raw, int):
            excluded = excluded_raw

    return DailyReport(
        date=date,
        mode=mode,
        dry_run=dry_run,
        discovery_chosen=discovery_chosen,
        discovery_blocked=discovery_blocked,
        market_gates=market_gates,
        buys=buys,
        liquidations=liquidations,
        error_count=error_count,
        note_ko=note_ko,
        realized_pnl=realized_pnl,
        realized_pnl_pct=realized_pnl_pct,
        sells=sells,
        excluded_etf_count=excluded,
        rank_candidates=rank_candidates,
        discovery_chosen_names=chosen_names,
        closing_cash=closing_cash,
        closing_equity=closing_equity,
        closing_total_pnl=closing_total_pnl,
        closing_holdings=closing_holdings,
        total_fees=total_fees,
        total_taxes=total_taxes,
        total_slippage=total_slippage,
        realized_pnl_after_cost=realized_pnl_after_cost,
        timeline=timeline,
    )


def _extract_discovery(events: LogEvents) -> tuple[list[str], list[tuple[str, str]]]:
    if events.discovery is None:
        return [], []
    chosen_raw = events.discovery.get("chosen") or []
    chosen: list[str] = [str(s) for s in chosen_raw if isinstance(s, str)]
    blocked_raw = events.discovery.get("blocked") or []
    blocked: list[tuple[str, str]] = []
    for item in blocked_raw:
        if isinstance(item, list | tuple) and len(item) >= 2:
            blocked.append((str(item[0]), str(item[1])))
    return chosen, blocked


def _extract_gates(events: LogEvents) -> list[MarketGateInfo]:
    out: list[MarketGateInfo] = []
    seen: set[str] = set()
    for rec in events.market_gates:
        market = str(rec.get("market", ""))
        if not market or market in seen:
            continue
        seen.add(market)
        out.append(
            MarketGateInfo(
                market=market,
                last=_as_float(rec.get("last")),
                sma5=_as_float(rec.get("sma5")),
                sma20=_as_float(rec.get("sma20")),
                above=bool(rec.get("above")),
            )
        )
    return out


def _extract_trades(
    events: LogEvents, names: dict[str, str]
) -> tuple[list[TradeRecord], list[TradeRecord], list[LiquidationRecord]]:
    """로그 → (매수, 장중매도, EOD청산).

    2026-05-16: side=="sell" 주문(stop-loss / trailing / partial-take)을 sells 로
    별도 추출. 이전엔 buy 만 보고 SELL 을 버려 realized_pnl 이 항상 None 이었다.
    EOD 강제청산(runner.liquidate_*)은 이벤트명이 달라 sells 와 겹치지 않는다.
    """
    buys: list[TradeRecord] = []
    sells: list[TradeRecord] = []
    buy_prices: dict[str, float] = {}  # symbol -> last buy price (시뮬·실거래 공통)
    for rec in events.orders_simulated:
        side = str(rec.get("side", ""))
        symbol = str(rec.get("symbol", ""))
        qty = _as_int(rec.get("qty")) or 0
        price = _as_float(rec.get("price"))
        if not symbol or qty <= 0:
            continue
        if side == "buy":
            buys.append(
                TradeRecord(symbol, "buy", qty, price, True, names.get(symbol))
            )
            if price is not None:
                buy_prices[symbol] = price
        elif side == "sell":
            sells.append(
                TradeRecord(symbol, "sell", qty, price, True, names.get(symbol))
            )
    for rec in events.orders_accepted:
        side = str(rec.get("side", ""))
        symbol = str(rec.get("symbol", ""))
        qty = _as_int(rec.get("qty")) or 0
        # 신규: 러너가 봉 종가로 근사한 체결가를 함께 로깅 (시장가 체결가 사후 보정 X).
        price = _as_float(rec.get("price"))
        if not symbol or qty <= 0:
            continue
        if side == "buy":
            buys.append(
                TradeRecord(symbol, "buy", qty, price, False, names.get(symbol))
            )
            if price is not None:
                buy_prices[symbol] = price
        elif side == "sell":
            sells.append(
                TradeRecord(symbol, "sell", qty, price, False, names.get(symbol))
            )

    liqs: list[LiquidationRecord] = []
    for rec in events.liquidations_simulated:
        symbol = str(rec.get("symbol", ""))
        qty = _as_int(rec.get("qty")) or 0
        if symbol and qty > 0:
            buy_price = _as_float(rec.get("buy_price")) or buy_prices.get(symbol)
            liqs.append(
                LiquidationRecord(
                    symbol=symbol,
                    quantity=qty,
                    sell_price=_as_float(rec.get("sell_price")),
                    buy_price=buy_price,
                    simulated=True,
                    name=names.get(symbol),
                )
            )
    for rec in events.liquidations_accepted:
        symbol = str(rec.get("symbol", ""))
        qty = _as_int(rec.get("qty")) or 0
        if symbol and qty > 0:
            buy_price = _as_float(rec.get("buy_price")) or buy_prices.get(symbol)
            liqs.append(
                LiquidationRecord(
                    symbol=symbol,
                    quantity=qty,
                    sell_price=_as_float(rec.get("sell_price")),
                    buy_price=buy_price,
                    simulated=False,
                    name=names.get(symbol),
                )
            )
    return buys, sells, liqs


def _extract_closing(
    snapshot: RunnerStateSnapshot | None,
) -> tuple[int | None, int | None, int | None, list[ClosingHolding]]:
    """러너 종료 시 인메모리 잔고 → 마감 잔고 필드들. snapshot 없으면 모두 비움.

    snapshot.snapshot 이 None 이면(잔고 폴백 실패 등) 마감 잔고도 채우지 않음.
    숫자는 키움 Decimal 을 정수 won 으로 떨어뜨림 — JSON 직렬화·표시 둘 다 단순.
    """
    if snapshot is None or snapshot.snapshot is None:
        return None, None, None, []
    raw = snapshot.snapshot
    cash = int(raw.deposit)
    equity = int(raw.total_evaluation)
    total_pnl = int(raw.total_pnl)
    holdings = [
        ClosingHolding(
            symbol=h.symbol,
            name=h.name,
            quantity=int(h.quantity),
            avg_price=float(h.avg_price),
            current_price=float(h.current_price),
            market_value=float(h.market_value),
            pnl=float(h.pnl),
            pnl_pct=float(h.pnl_pct),
        )
        for h in raw.holdings
        if int(h.quantity) > 0  # 청산 직후 0짜리 잔재는 거름
    ]
    return cash, equity, total_pnl, holdings


def _estimate_costs(
    buys: list[TradeRecord],
    sells: list[TradeRecord],
    liqs: list[LiquidationRecord],
) -> tuple[float | None, float | None, float | None]:
    """매수·청산 가격이 모두 있을 때 수수료·거래세·슬리피지 추정치를 합산.

    무엇/왜:
    - mock 모드는 키움 시뮬이 비용 0 으로 응답하므로, 사용자에게 "실거래라면 이만큼 빠진다"
      를 보여주기 위해 추정. 실거래에서도 교차 검증 차원에서 같은 추정치를 함께 저장.
    - 가격이 하나라도 빠진 거래가 있으면 None 셋을 반환 — 일부만 추정한 값은 오히려 오해.

    어떻게 확인: tests/test_reports_daily.py 의 비용 차감 PnL 케이스에서 정확한 값 검증.
    """
    if not buys and not sells and not liqs:
        return None, None, None

    fees = 0.0
    taxes = 0.0
    slippage = 0.0

    for trade in buys:
        if trade.price is None:
            return None, None, None
        notional = trade.price * trade.quantity
        fees += notional * COST_COMMISSION_RATE
        slippage += notional * (COST_SLIPPAGE_BPS / 10_000.0)

    for trade in sells:
        if trade.price is None:
            return None, None, None
        notional = trade.price * trade.quantity
        fees += notional * COST_COMMISSION_RATE
        taxes += notional * COST_TAX_RATE_SELL
        slippage += notional * (COST_SLIPPAGE_BPS / 10_000.0)

    for li in liqs:
        # 청산 가격은 없을 수 있으나(시장가) 매수가는 있어야 추정 가능.
        # 추정 단가는 sell_price 우선, 없으면 buy_price 사용 (~ 동일 종목 일중 변동 무시 근사).
        unit = li.sell_price if li.sell_price is not None else li.buy_price
        if unit is None:
            return None, None, None
        notional = unit * li.quantity
        fees += notional * COST_COMMISSION_RATE
        taxes += notional * COST_TAX_RATE_SELL
        slippage += notional * (COST_SLIPPAGE_BPS / 10_000.0)

    return fees, taxes, slippage


def _aggregate_realized(
    buys: list[TradeRecord],
    sells: list[TradeRecord],
    liqs: list[LiquidationRecord],
) -> tuple[float | None, float | None]:
    """종목별 FIFO 로 매수↔(장중매도+EOD청산) 라운드트립 실현손익 집계.

    무엇/왜:
    - 2026-05-16: 이전 `_aggregate_pnl` 은 EOD liquidations 만 봤고, 게다가
      "하나라도 가격 없으면 전부 None" 이라 EOD 청산의 sell_price 가 None(시장가)
      이면 그날 실현손익이 통째로 사라졌다. 장중 stop/trailing/partial 매도는
      아예 집계 대상도 아니었음 → 주간 인사이트가 매번 "성과 검증 불가" 로 오판.
    - 새 규칙: 매수를 종목별 FIFO 큐로 쌓고 매도/청산이 순서대로 소진. 매수가·
      매도가가 **둘 다** 있는 체결분만 손익에 합산하고, 한쪽 가격이 없는 분량은
      재고만 소진하고 손익에서 제외(부분 가시성 — 전부 버리지 않음). 가격이
      잡힌 매칭이 0이면 (None, None).
    - 청산가 우선순위: li.sell_price → 없으면 손익 미산정(매수가만으론 불가).
      매수가 우선순위: FIFO 큐의 매수가 → 큐가 비면 li.buy_price(로그 필드).

    리포트는 사후 추정이라 정확 체결가가 아닌 봉 종가 근사를 쓸 수 있다(주석 범위).
    """
    queues: dict[str, list[list[float | None]]] = {}  # symbol -> [[qty, price], ...]
    for b in buys:
        if b.quantity > 0:
            queues.setdefault(b.symbol, []).append([float(b.quantity), b.price])

    total_pnl = 0.0
    total_cost = 0.0
    matched_any = False

    def _drain(symbol: str, qty: float) -> None:
        """FIFO 큐에서 qty 만큼 재고만 소진(손익 계산 없이) — 명시 buy_price 사용 시."""
        q = queues.get(symbol) or []
        remaining = qty
        while remaining > 0 and q:
            lot = q[0]
            lot_qty = lot[0] or 0.0
            take = min(remaining, lot_qty)
            remaining -= take
            lot[0] = lot_qty - take
            if (lot[0] or 0.0) <= 0:
                q.pop(0)

    def _consume(symbol: str, qty: int, sell_price: float | None, liq_buy: float | None) -> None:
        nonlocal total_pnl, total_cost, matched_any
        # EOD 청산이 자체 매수단가(브로커 평균원가)를 명시하면 그것을 권위로 삼고
        # FIFO 큐는 재고만 소진 — buys 매핑보다 우선(문서화된 precedence 유지).
        if liq_buy is not None:
            if sell_price is not None:
                total_pnl += (sell_price - liq_buy) * qty
                total_cost += liq_buy * qty
                matched_any = True
            _drain(symbol, float(qty))
            return
        remaining = float(qty)
        q = queues.get(symbol) or []
        while remaining > 0 and q:
            lot = q[0]
            lot_qty = lot[0] or 0.0
            take = min(remaining, lot_qty)
            buy_price = lot[1]
            if buy_price is not None and sell_price is not None:
                total_pnl += (sell_price - buy_price) * take
                total_cost += buy_price * take
                matched_any = True
            remaining -= take
            lot[0] = lot_qty - take
            if (lot[0] or 0.0) <= 0:
                q.pop(0)

    for s in sells:
        if s.quantity > 0:
            _consume(s.symbol, s.quantity, s.price, None)
    for li in liqs:
        if li.quantity > 0:
            _consume(li.symbol, li.quantity, li.sell_price, li.buy_price)

    if not matched_any or total_cost == 0:
        return None, None
    return total_pnl, total_pnl / total_cost * 100.0


def _summary_ko(
    *,
    chosen: list[str],
    buys: list[TradeRecord],
    sells: list[TradeRecord],
    blocked: list[tuple[str, str]],
    liquidations: list[LiquidationRecord],
    realized_pnl: float | None,
    realized_pnl_after_cost: float | None,
    error_count: int,
) -> str:
    """note_ko 빌더. 2026-05-11 회귀: 발굴(`chosen`)과 실제 체결(`buys`) 을 구분.

    - 기존 동작: `매수 N건` 의 N 을 `len(chosen)` 으로 표기 → kill switch / 게이트로 매수
      실패한 날에도 "매수 3건" 으로 찍혀 사용자·주간 인사이트 LLM 둘 다 오독했음.
    - 새 동작: `매수 N건` 의 N 은 실제 체결된 `buys` 기준. 발굴은 됐는데 체결 0건이면
      "발굴 N건 (체결 0)" 로 분리 표기 → buys 배열이 비어있다는 사실을 메모와 일치시킴.
    """
    if not chosen and not liquidations and not buys and not sells:
        if blocked:
            return f"매수 후보 {len(blocked)}건 모두 게이트 차단 — 거래 없음."
        return "거래 없음."
    parts: list[str] = []
    if buys:
        # 중복 종목 제거(같은 종목 분할 매수도 1건으로 — 발굴 카운트와 일관성).
        seen: set[str] = set()
        buy_syms: list[str] = []
        for trade in buys:
            if trade.symbol not in seen:
                seen.add(trade.symbol)
                buy_syms.append(trade.symbol)
        parts.append(
            f"매수 {len(buy_syms)}건 ({', '.join(buy_syms[:3])}{'…' if len(buy_syms) > 3 else ''})"
        )
    elif chosen:
        # 발굴은 됐지만 실제 매수 0건 — 메모와 buys 배열의 정합성 유지.
        parts.append(f"발굴 {len(chosen)}건 (체결 0)")
    if sells:
        parts.append(f"매도 {len(sells)}건")
    if liquidations:
        parts.append(f"청산 {len(liquidations)}건")
    if realized_pnl is not None:
        sign = "+" if realized_pnl >= 0 else ""
        pnl_part = f"실현 {sign}{int(realized_pnl):,}원"
        # 비용 차감치도 같이 보여줌 — 실거래 수익 가시화.
        if realized_pnl_after_cost is not None and realized_pnl_after_cost != realized_pnl:
            sign_c = "+" if realized_pnl_after_cost >= 0 else ""
            pnl_part += f" (비용 차감 {sign_c}{int(realized_pnl_after_cost):,}원)"
        parts.append(pnl_part)
    if error_count > 0:
        parts.append(f"에러 {error_count}건")
    return " · ".join(parts)


# 2026-05-11 회귀 후속 — 시계열 한 줄 빌더.
# UTC ISO 8601 → KST "HH:MM". structlog TimeStamper(fmt="iso") 출력 호환.
# 실패하면 None — 빌더가 그 이벤트를 timeline 에서 skip.
def _ts_to_kst_hhmm(raw: object) -> str | None:
    if not isinstance(raw, str) or not raw:
        return None
    from datetime import datetime, timedelta, timezone

    # structlog 의 iso 포맷은 "...Z" / "...+00:00" / 둘 다 발생 가능.
    try:
        normalized = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    kst = dt.astimezone(timezone(timedelta(hours=9)))
    return kst.strftime("%H:%M")


def _with_name_label(symbol: str, names: dict[str, str]) -> str:
    """'005930 삼성전자' 형식. name 없으면 코드만."""
    name = names.get(symbol)
    return f"{symbol} {name}" if name else symbol


def _format_won_int(value: object) -> str:
    """천 단위 콤마 + '원'. 변환 실패면 '?원'."""
    if value is None:
        return "?원"
    try:
        return f"{round(float(value)):,}원"  # type: ignore[arg-type]
    except TypeError, ValueError:
        return "?원"


_KILL_SWITCH_REASON_KO = {"daily_loss_kill": "일일 손실 한도"}
_LIQUIDATE_REASON_KO = {
    "liquidate_at_reached": "장 마감",
    "daily_loss_kill": "일일 손실 한도",
}


def _build_timeline(events: LogEvents, names: dict[str, str]) -> list[TimelineEvent]:
    """LogEvents → 시각 순으로 정렬된 한 줄 메시지 리스트.

    포함 이벤트(시각 있는 것만):
    - 발굴 완료 (daytrade.discovery)
    - 매수 체결 (runner.order_simulated / runner.order_accepted) — side=buy 만
    - 부분 익절 (position_guard.partial_take)
    - kill switch (runner.kill_switch_triggered)
    - 청산 시작 (runner.liquidate_start)

    체결 가격은 시뮬에선 봉 종가 근사. 사용자에게 "실제 체결가가 아닐 수 있음" 은 코드 주석
    범위 — UI 에서 따로 표시하지 않음.
    """
    items: list[tuple[str, TimelineEvent]] = []  # (raw_ts, event) — 정렬 키 보존.

    def _add(raw_ts: object, kind: str, text: str) -> None:
        hhmm = _ts_to_kst_hhmm(raw_ts)
        if hhmm is None:
            return
        key = raw_ts if isinstance(raw_ts, str) else hhmm
        items.append((key, TimelineEvent(time_kst=hhmm, kind=kind, text=text)))

    if events.discovery is not None:
        chosen_raw = events.discovery.get("chosen") or []
        chosen = [str(s) for s in chosen_raw if isinstance(s, str)]
        suffix = ""
        if chosen:
            shown = ", ".join(_with_name_label(s, names) for s in chosen[:3])
            tail = f" 외 {len(chosen) - 3}" if len(chosen) > 3 else ""
            suffix = f": {shown}{tail}"
        _add(
            events.discovery.get("timestamp"),
            "discovery",
            f"발굴 채택 {len(chosen)}건{suffix}",
        )

    # 매수는 시뮬·실거래 모두 동일하게 한 줄.
    for rec in events.orders_simulated:
        if str(rec.get("side", "")) != "buy":
            continue
        symbol = str(rec.get("symbol", ""))
        qty = _as_int(rec.get("qty")) or 0
        price = _as_float(rec.get("price"))
        price_part = f" @{_format_won_int(price)}" if price is not None else ""
        _add(
            rec.get("timestamp"),
            "buy",
            f"매수 {_with_name_label(symbol, names)} {qty}주{price_part} (시뮬)",
        )
    for rec in events.orders_accepted:
        if str(rec.get("side", "")) != "buy":
            continue
        symbol = str(rec.get("symbol", ""))
        qty = _as_int(rec.get("qty")) or 0
        price = _as_float(rec.get("price"))
        price_part = f" @{_format_won_int(price)}" if price is not None else ""
        _add(
            rec.get("timestamp"),
            "buy",
            f"매수 {_with_name_label(symbol, names)} {qty}주{price_part}",
        )

    for rec in events.partial_takes:
        symbol = str(rec.get("symbol", ""))
        qty = _as_int(rec.get("qty_to_sell")) or 0
        gain_pct = _as_float(rec.get("gain_pct"))
        price = _as_float(rec.get("price"))
        gain_part = f" +{gain_pct:.2f}%" if gain_pct is not None else ""
        price_part = f" @{_format_won_int(price)}" if price is not None else ""
        _add(
            rec.get("timestamp"),
            "partial_take",
            f"부분 익절 {_with_name_label(symbol, names)} {qty}주{price_part}{gain_part}",
        )

    for rec in events.kill_switches:
        change = _as_float(rec.get("today_change"))
        limit = _as_float(rec.get("daily_loss_limit"))
        change_pct = f"{change * 100:+.2f}%" if change is not None else "?"
        limit_pct = f"-{limit * 100:.1f}%" if limit is not None else "?"
        _add(
            rec.get("timestamp"),
            "kill_switch",
            f"🛑 일일 손실 한도 도달 (당일 {change_pct}, 한도 {limit_pct}) — 강제 청산",
        )

    for rec in events.liquidate_starts:
        reason = str(rec.get("reason", ""))
        reason_ko = _LIQUIDATE_REASON_KO.get(reason, reason or "청산")
        symbols_raw = rec.get("symbols") or []
        symbols = [str(s) for s in symbols_raw if isinstance(s, str)]
        if symbols:
            shown = ", ".join(_with_name_label(s, names) for s in symbols[:3])
            tail = f" 외 {len(symbols) - 3}" if len(symbols) > 3 else ""
            text = f"청산 시작 ({reason_ko}): {shown}{tail}"
        else:
            text = f"청산 시작 ({reason_ko}) — 보유 없음"
        _add(rec.get("timestamp"), "liquidate", text)

    # raw timestamp 문자열 정렬 = ISO 8601 사전식 정렬 = 시각 정렬.
    items.sort(key=lambda pair: pair[0])
    return [ev for _, ev in items]


def _as_float(raw: object) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return None


def _as_int(raw: object) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)  # type: ignore[call-overload]
    except TypeError, ValueError:
        return None


# ---- I/O -----------------------------------------------------------------


def save_daily_report(report: DailyReport, *, root: Path | str = "data/reports") -> Path:
    """`<root>/daily-<date>.json` 으로 저장. 디렉터리 자동 생성."""
    out_dir = Path(root)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"daily-{report.date}.json"
    payload = _to_json_dict(report)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


def load_daily_report(path: Path | str) -> DailyReport:
    """저장된 JSON → DailyReport 라운드트립."""
    raw = Path(path).read_text(encoding="utf-8")
    payload = json.loads(raw)
    return _from_json_dict(payload)


def _to_json_dict(report: DailyReport) -> dict[str, Any]:
    d = asdict(report)
    # tuple → list (JSON 호환)
    d["discovery_blocked"] = [list(t) for t in report.discovery_blocked]
    return d


def _from_json_dict(payload: dict[str, Any]) -> DailyReport:
    chosen_names_raw = payload.get("discovery_chosen_names") or {}
    chosen_names = {str(k): str(v) for k, v in chosen_names_raw.items() if isinstance(k, str)}
    closing_holdings_raw = payload.get("closing_holdings") or []
    closing_holdings = [ClosingHolding(**h) for h in closing_holdings_raw]
    timeline_raw = payload.get("timeline") or []
    timeline = [TimelineEvent(**ev) for ev in timeline_raw if isinstance(ev, dict)]
    return DailyReport(
        date=payload["date"],
        mode=payload["mode"],
        dry_run=payload["dry_run"],
        discovery_chosen=list(payload.get("discovery_chosen") or []),
        discovery_blocked=[
            (str(t[0]), str(t[1]))
            for t in (payload.get("discovery_blocked") or [])
            if isinstance(t, list | tuple) and len(t) >= 2
        ],
        market_gates=[MarketGateInfo(**g) for g in payload.get("market_gates") or []],
        buys=[TradeRecord(**t) for t in payload.get("buys") or []],
        liquidations=[LiquidationRecord(**li) for li in payload.get("liquidations") or []],
        error_count=int(payload.get("error_count") or 0),
        note_ko=str(payload.get("note_ko") or ""),
        realized_pnl=payload.get("realized_pnl"),
        realized_pnl_pct=payload.get("realized_pnl_pct"),
        sells=[TradeRecord(**t) for t in payload.get("sells") or []],
        excluded_etf_count=int(payload.get("excluded_etf_count") or 0),
        rank_candidates=list(payload.get("rank_candidates") or []),
        discovery_chosen_names=chosen_names,
        closing_cash=payload.get("closing_cash"),
        closing_equity=payload.get("closing_equity"),
        closing_total_pnl=payload.get("closing_total_pnl"),
        closing_holdings=closing_holdings,
        total_fees=payload.get("total_fees"),
        total_taxes=payload.get("total_taxes"),
        total_slippage=payload.get("total_slippage"),
        realized_pnl_after_cost=payload.get("realized_pnl_after_cost"),
        timeline=timeline,
    )


def merge_symbol_names(reports: Iterable[DailyReport]) -> dict[str, str]:
    """여러 DailyReport 의 discovery_chosen_names 를 하나로 병합 → 코드→종목명.

    주간 단계(인사이트·프로파일 제안)가 LLM 입력/출력에서 종목을 코드만 보던 문제를
    고치기 위한 공용 헬퍼. 같은 코드가 여러 날 다른 이름이면 마지막(최신)이 우선.
    """
    out: dict[str, str] = {}
    for r in reports:
        for sym, name in r.discovery_chosen_names.items():
            if sym and isinstance(name, str) and name.strip():
                out[sym] = name.strip()
    return out


def with_symbol_name(symbol: str, name: str | None) -> str:
    """'005930 삼성전자' 형식 — 이름이 없으면 코드만 (notify/state·telegram 와 동일 폴백)."""
    if name and name.strip():
        return f"{symbol} {name.strip()}"
    return symbol


__all__ = [
    "ClosingHolding",
    "DailyReport",
    "LiquidationRecord",
    "MarketGateInfo",
    "TimelineEvent",
    "TradeRecord",
    "build_daily_report",
    "load_daily_report",
    "merge_symbol_names",
    "save_daily_report",
    "with_symbol_name",
]
