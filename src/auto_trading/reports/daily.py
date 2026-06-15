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
from .snapshot_reader import TopcapSnapshots, parse_topcap_for_date

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
    # 데이터 기반 분석용 — 체결 시각(KST "HH:MM", 로그 timestamp 변환)과 사유.
    # 매수 reason = 전략 Signal.reason, 매도 reason = guard 사유(손절/트레일링/부분익절)
    # 또는 전략 매도 사유. 로그에 없으면 None.
    time_kst: str | None = None
    reason: str | None = None
    # 진입 시점 피처(매수 레코드만) — topcap 스냅샷 조인. 스냅샷 10분 캐던스라 근사.
    change_pct_at_entry: float | None = None  # 진입 무렵 종목 등락률(%)
    rank_at_entry: int | None = None  # 진입 무렵 시총 순위


@dataclass(frozen=True)
class LiquidationRecord:
    """청산 1건. 매수 가격까지 알면 buy_price 채움 → pnl 계산."""

    symbol: str
    quantity: int
    sell_price: float | None
    buy_price: float | None
    simulated: bool
    name: str | None = None
    # EOD 청산 시각(KST "HH:MM")과 사유("liquidate_at_reached"/"daily_loss_kill" 등).
    time_kst: str | None = None
    reason: str | None = None

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
class RoundTrip:
    """종목 왕복(매수→매도/청산) 1건 — 데이터 기반 전략 분석의 기본 단위.

    FIFO 매칭으로 매수 lot 과 매도/청산을 짝지어 만든다. 매수가·매도가가 둘 다
    있는 분량만 손익이 채워진다(가격 결손 분은 pnl=None). 승률·손익비·보유시간·
    사유별 성과 분석에 그대로 쓰인다.
    """

    symbol: str
    name: str | None
    quantity: int
    buy_price: float | None
    sell_price: float | None
    buy_time_kst: str | None
    sell_time_kst: str | None
    holding_minutes: int | None
    pnl: float | None
    pnl_pct: float | None
    exit_reason: str | None  # guard 사유 / "eod" 계열 / 전략 매도 사유
    # 보유구간 미실현 최대이익/최대손실(%) — topcap 스냅샷 조인. 매수가 기준.
    # "익절을 너무 빨리/늦게 했나" 진단용. 스냅샷 10분 캐던스라 거친 추정.
    mfe_pct: float | None = None
    mae_pct: float | None = None


@dataclass(frozen=True)
class BlockedOutcome:
    """발굴에서 차단된 종목의 그날 실제 움직임 — 게이트 기회비용(반사실) 분석용.

    "차단 안 했으면 진입했을 텐데" 의 사후 검증: 진입했다면 일중(시초→종가)·
    일간(전일대비) 수익률이 얼마였는지. topcap 스냅샷에 그 종목이 없으면 None.
    """

    symbol: str
    name: str | None
    reason: str  # 차단 사유 (breadth_* / *_below_sma / avoid_symbols 등)
    intraday_pct: float | None  # 그날 시초→종가 변화율(%)
    day_change_pct: float | None  # 종가 기준 전일대비 등락률(%)


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
    # "discovery" | "buy" | "sell" | "partial_take" | "stop_loss" | "trailing"
    # | "kill_switch" | "liquidate"
    kind: str
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
    # 과매매 가시화 — 손실 핵심 원인이 장중 과회전이라 사용자가 눈으로 보게 한다.
    # traded_notional: 전 체결 거래대금 합(가격 없는 체결은 제외, 전부 없으면 None).
    # turnover_ratio: 거래대금 ÷ 당일 시작자본. snapshot.account.day_start_equity 가
    #   있을 때만 — 분모 추정 금지(없으면 None).
    traded_notional: float | None = None
    turnover_ratio: float | None = None
    # error_count 분해 — "에러 N건" 통짜는 진단 불가. 0 인 항목은 표시단에서 생략.
    orders_failed_count: int = 0
    liquidations_failed_count: int = 0
    balance_failure_count: int = 0
    fatal_count: int = 0
    # === 데이터 기반 전략 수립용 맥락(2026-06-15) ===
    # 그날 시장 국면(daytrade.regime 로그) — regime_router 활성일 때만, 아니면 None.
    regime: str | None = None
    # 시총200 breadth 게이트 측정치(daytrade.breadth_gate 로그). 게이트 비활성이면 None.
    breadth_advancing_pct: float | None = None
    breadth_mean_change_pct: float | None = None
    breadth_passed: bool | None = None
    # 종목 왕복(매수↔매도/청산) 단위 손익 — 승률·보유시간·사유별 성과 분석의 기본 재료.
    round_trips: list[RoundTrip] = field(default_factory=list)
    # 차단 종목의 그날 움직임 — 게이트 기회비용(반사실) 분석. 스냅샷 없으면 빈 리스트.
    blocked_outcomes: list[BlockedOutcome] = field(default_factory=list)


# ---- 빌더 ---------------------------------------------------------------


def build_daily_report(
    *,
    date: str,
    events: LogEvents,
    snapshot: RunnerStateSnapshot | None,
    snapshot_root: Path | str = "data/snapshots",
) -> DailyReport:
    """LogEvents + 옵셔널 snapshot 으로 DailyReport 생성.

    무엇/왜:
    - 자동 호출(러너 종료 직후) 은 snapshot 이 있어 mode/dry_run 을 알 수 있음.
    - 수동 재생성(`auto-trade report --date …`) 은 snapshot=None — 로그만 보고 추정.
    - day-trade 가 아닌 일자(레거시 swing 또는 데이터 없음) 도 빈 DailyReport 로 안전하게 만듦.
    - snapshot_root: topcap 일중 스냅샷 폴더. 파일 없으면 진입 피처·MFE/MAE·차단
      기회비용은 모두 None/빈 리스트로 폴백(과거 일자 재생성 호환).
    """
    discovery_chosen, discovery_blocked = _extract_discovery(events)
    market_gates = _extract_gates(events)
    names = dict(events.symbol_names)
    buys, sells, liquidations = _extract_trades(events, names)
    orders_failed_count = len(events.orders_failed)
    liquidations_failed_count = len(events.liquidations_failed)
    fatal_count = len(events.fatal)
    balance_failure_count = 1 if events.balance_failures > 0 else 0
    error_count = (
        orders_failed_count + liquidations_failed_count + fatal_count + balance_failure_count
    )

    # snapshot 우선. 없으면 events 로 mode/dry_run 추정.
    if snapshot is not None:
        mode = snapshot.runner_mode
        dry_run = snapshot.dry_run
    else:
        mode = "day-trade" if (events.discovery is not None or events.rank is not None) else "swing"
        dry_run = bool(events.orders_simulated or events.liquidations_simulated)

    realized_pnl, realized_pnl_pct, round_trips = _aggregate_realized(buys, sells, liquidations)
    regime, breadth_adv, breadth_mean, breadth_passed = _extract_regime_breadth(events)
    # topcap 일중 스냅샷 조인 — 진입 피처·MFE/MAE·차단 기회비용. 파일 없으면 무영향.
    snaps = parse_topcap_for_date(date, root=snapshot_root)
    buys = _enrich_buys(buys, snaps)
    round_trips = _enrich_round_trips(round_trips, snaps)
    blocked_outcomes = _build_blocked_outcomes(discovery_blocked, names, snaps)
    closing_cash, closing_equity, closing_total_pnl, closing_holdings = _extract_closing(snapshot)
    # snapshot.holdings 의 name 도 흡수 — discovery 에 없던 종목(swing 등) 보강.
    if snapshot is not None and snapshot.snapshot is not None:
        for h in snapshot.snapshot.holdings:
            if h.name and h.symbol not in names:
                names[h.symbol] = h.name
    # chosen 뿐 아니라 blocked 후보 코드도 매핑 — 리포트 "차단:" 라인이 코드+이름으로
    # 보이게. daytrade.discovery 로그가 전 후보 names 를 남기므로 events 에 다 있음.
    discovery_syms = list(discovery_chosen) + [s for s, _ in discovery_blocked]
    chosen_names = {sym: names[sym] for sym in discovery_syms if sym in names}
    total_fees, total_taxes, total_slippage = _estimate_costs(buys, sells, liquidations)
    realized_pnl_after_cost = (
        realized_pnl - (total_fees + total_taxes + total_slippage)
        if realized_pnl is not None
        and total_fees is not None
        and total_taxes is not None
        and total_slippage is not None
        else None
    )

    traded_notional = _aggregate_notional(buys, sells, liquidations)
    day_start_equity = (
        float(snapshot.account.day_start_equity)
        if snapshot is not None
        and snapshot.account is not None
        and snapshot.account.day_start_equity > 0
        else None
    )
    turnover_ratio = (
        traded_notional / day_start_equity
        if traded_notional is not None and day_start_equity
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
        traded_notional=traded_notional,
        turnover_ratio=turnover_ratio,
        orders_failed_count=orders_failed_count,
        liquidations_failed_count=liquidations_failed_count,
        balance_failure_count=balance_failure_count,
        fatal_count=fatal_count,
        regime=regime,
        breadth_advancing_pct=breadth_adv,
        breadth_mean_change_pct=breadth_mean,
        breadth_passed=breadth_passed,
        round_trips=round_trips,
        blocked_outcomes=blocked_outcomes,
    )


def _enrich_buys(buys: list[TradeRecord], snaps: TopcapSnapshots) -> list[TradeRecord]:
    """매수 레코드에 진입 시점 등락률·순위(스냅샷 조인)를 채운다. 결손은 None 유지."""
    from dataclasses import replace

    out: list[TradeRecord] = []
    for b in buys:
        out.append(
            replace(
                b,
                change_pct_at_entry=snaps.change_pct_at(b.symbol, b.time_kst),
                rank_at_entry=snaps.rank_at(b.symbol, b.time_kst),
            )
        )
    return out


def _enrich_round_trips(round_trips: list[RoundTrip], snaps: TopcapSnapshots) -> list[RoundTrip]:
    """왕복 레코드에 보유구간 MFE/MAE(%)를 채운다. 매수가·구간 결손이면 None 유지."""
    from dataclasses import replace

    out: list[RoundTrip] = []
    for rt in round_trips:
        rng = snaps.range_during(rt.symbol, rt.buy_time_kst, rt.sell_time_kst)
        mfe = mae = None
        if rng is not None and rt.buy_price:
            low, high = rng
            mfe = (high / rt.buy_price - 1.0) * 100.0
            mae = (low / rt.buy_price - 1.0) * 100.0
        out.append(replace(rt, mfe_pct=mfe, mae_pct=mae))
    return out


def _build_blocked_outcomes(
    discovery_blocked: list[tuple[str, str]],
    names: dict[str, str],
    snaps: TopcapSnapshots,
) -> list[BlockedOutcome]:
    """차단 종목별 그날 움직임(시초→종가·전일대비) — 게이트 기회비용 반사실."""
    out: list[BlockedOutcome] = []
    for symbol, reason in discovery_blocked:
        intraday, day_change = snaps.day_outcome(symbol)
        out.append(
            BlockedOutcome(
                symbol=symbol,
                name=names.get(symbol),
                reason=reason,
                intraday_pct=intraday,
                day_change_pct=day_change,
            )
        )
    return out


def _extract_regime_breadth(
    events: LogEvents,
) -> tuple[str | None, float | None, float | None, bool | None]:
    """daytrade.regime / daytrade.breadth_gate 로그 → 그날 시장 맥락 4종.

    각 로그는 세션 시작 시 1건만 나온다. 없으면 모두 None(게이트·라우팅 비활성).
    """
    regime = _as_str(events.regime.get("regime")) if events.regime is not None else None
    if events.breadth_gate is None:
        return regime, None, None, None
    bg = events.breadth_gate
    passed_raw = bg.get("passed")
    passed = bool(passed_raw) if isinstance(passed_raw, bool) else None
    return (
        regime,
        _as_float(bg.get("advancing_pct")),
        _as_float(bg.get("mean_change_pct")),
        passed,
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


def _collect_order_records(
    records: list[dict],
    *,
    simulated: bool,
    names: dict[str, str],
    buys: list[TradeRecord],
    sells: list[TradeRecord],
    buy_prices: dict[str, float],
) -> None:
    """주문 이벤트 한 묶음(시뮬 또는 실체결)을 buys/sells 에 분배.

    시뮬(orders_simulated)과 실체결(orders_accepted)이 ``simulated`` 플래그만
    다르고 로직은 동일해 한 곳으로 모음. buy_prices 는 호출 간 공유돼 마지막
    매수가가 청산 buy_price 폴백으로 쓰인다.
    """
    for rec in records:
        side = str(rec.get("side", ""))
        symbol = str(rec.get("symbol", ""))
        qty = _as_int(rec.get("qty")) or 0
        # 러너가 봉 종가로 근사한 체결가를 함께 로깅 (시장가 체결가 사후 보정 X).
        price = _as_float(rec.get("price"))
        if not symbol or qty <= 0:
            continue
        time_kst = _ts_to_kst_hhmm(rec.get("timestamp"))
        if side == "buy":
            # 진입 사유 = Signal.reason (runner.order_* 가 함께 로깅).
            buys.append(
                TradeRecord(
                    symbol,
                    "buy",
                    qty,
                    price,
                    simulated,
                    names.get(symbol),
                    time_kst=time_kst,
                    reason=_as_str(rec.get("reason")),
                )
            )
            if price is not None:
                buy_prices[symbol] = price
        elif side == "sell":
            # 매도 사유 = guard 사유(dry-run 의 guard_reason) 또는 전략 매도 reason.
            reason = _as_str(rec.get("reason")) or _as_str(rec.get("guard_reason"))
            sells.append(
                TradeRecord(
                    symbol,
                    "sell",
                    qty,
                    price,
                    simulated,
                    names.get(symbol),
                    time_kst=time_kst,
                    reason=reason,
                )
            )


def _collect_liquidation_records(
    records: list[dict],
    *,
    simulated: bool,
    names: dict[str, str],
    buy_prices: dict[str, float],
    liqs: list[LiquidationRecord],
) -> None:
    """EOD 청산 이벤트 한 묶음을 liqs 에 누적. simulated/accepted 공용."""
    for rec in records:
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
                    simulated=simulated,
                    name=names.get(symbol),
                    time_kst=_ts_to_kst_hhmm(rec.get("timestamp")),
                    reason=_as_str(rec.get("reason")),
                )
            )


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
    _collect_order_records(
        events.orders_simulated,
        simulated=True,
        names=names,
        buys=buys,
        sells=sells,
        buy_prices=buy_prices,
    )
    _collect_order_records(
        events.orders_accepted,
        simulated=False,
        names=names,
        buys=buys,
        sells=sells,
        buy_prices=buy_prices,
    )
    _label_sell_reasons_from_guards(events, sells)

    liqs: list[LiquidationRecord] = []
    _collect_liquidation_records(
        events.liquidations_simulated,
        simulated=True,
        names=names,
        buy_prices=buy_prices,
        liqs=liqs,
    )
    _collect_liquidation_records(
        events.liquidations_accepted,
        simulated=False,
        names=names,
        buy_prices=buy_prices,
        liqs=liqs,
    )
    return buys, sells, liqs


def _label_sell_reasons_from_guards(events: LogEvents, sells: list[TradeRecord]) -> None:
    """reason 없는 매도 레코드를 position_guard 로그로 사유 라벨링.

    무엇/왜: 실거래 경로(runner.order_accepted) guard 매도는 reason 을 안 들고 온다
    (guard 사유는 별도 position_guard.* 로그에만 있음). timeline 빌더가 쓰는 것과
    같은 (symbol, price) 매칭으로 사유를 붙여 "사유별 성과" 분석을 가능케 한다.
    dry-run(guard_reason 이미 포함)·전략 매도(reason 포함)는 그대로 둔다.

    TradeRecord 는 frozen 이라 매칭된 항목을 dataclasses.replace 로 교체한다.
    """
    from dataclasses import replace

    # (symbol, price) → guard 사유 큐. 같은 키 다중 발화는 순서대로 소진.
    buckets: dict[tuple[str, float | None], list[str]] = {}
    for kind, recs in (
        ("partial_take", events.partial_takes),
        ("stop_loss", events.stop_losses),
        ("trailing", events.trailings),
    ):
        for rec in recs:
            key = (str(rec.get("symbol", "")), _as_float(rec.get("price")))
            buckets.setdefault(key, []).append(kind)
    if not buckets:
        return
    for i, trade in enumerate(sells):
        if trade.reason is not None:
            continue
        bucket = buckets.get((trade.symbol, trade.price))
        if bucket:
            sells[i] = replace(trade, reason=bucket.pop(0))


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


def _aggregate_notional(
    buys: list[TradeRecord],
    sells: list[TradeRecord],
    liqs: list[LiquidationRecord],
) -> float | None:
    """전 체결 거래대금(price*quantity) 합 — 과매매(회전율) 가시화용.

    _estimate_costs 와 달리 가격 없는 체결은 '제외'하고 가능한 만큼만 더한다.
    거래대금은 절대값 지표라 일부만 있어도 사용자에게 유의미(비용 추정과 성격 다름).
    더할 수 있는 체결이 하나도 없으면 None.
    """
    total = 0.0
    counted = 0
    for trade in (*buys, *sells):
        if trade.price is None:
            continue
        total += trade.price * trade.quantity
        counted += 1
    for li in liqs:
        unit = li.sell_price if li.sell_price is not None else li.buy_price
        if unit is None:
            continue
        total += unit * li.quantity
        counted += 1
    return total if counted else None


def _aggregate_realized(
    buys: list[TradeRecord],
    sells: list[TradeRecord],
    liqs: list[LiquidationRecord],
) -> tuple[float | None, float | None, list[RoundTrip]]:
    """종목별 FIFO 로 매수↔(장중매도+EOD청산) 라운드트립 실현손익 집계 + 왕복 레코드.

    무엇/왜:
    - 2026-05-16: 이전 `_aggregate_pnl` 은 EOD liquidations 만 봤고, 게다가
      "하나라도 가격 없으면 전부 None" 이라 EOD 청산의 sell_price 가 None(시장가)
      이면 그날 실현손익이 통째로 사라졌다. 장중 stop/trailing/partial 매도는
      아예 집계 대상도 아니었음 → 주간 인사이트가 매번 "성과 검증 불가" 로 오판.
    - 새 규칙: 매수를 종목별 FIFO 큐로 쌓고 매도/청산이 순서대로 소진. 매수가·
      매도가가 **둘 다** 있는 체결분만 손익에 합산하고, 한쪽 가격이 없는 분량은
      재고만 소진하고 손익에서 제외(부분 가시성 — 전부 버리지 않음). 가격이
      잡힌 매칭이 0이면 (None, None, [...]).
    - 청산가 우선순위: li.sell_price → 없으면 손익 미산정(매수가만으론 불가).
      매수가 우선순위: FIFO 큐의 매수가 → 큐가 비면 li.buy_price(로그 필드).
    - 2026-06-15: 매칭이 일어난 segment 마다 RoundTrip 을 부수적으로 수집한다
      (데이터 기반 전략 분석용). 합계 손익 계산은 종전과 동일 — 가격 결손 segment 도
      RoundTrip 으로 남되 pnl=None.

    리포트는 사후 추정이라 정확 체결가가 아닌 봉 종가 근사를 쓸 수 있다(주석 범위).
    """
    # symbol -> [[qty, price, time_kst, name], ...]
    queues: dict[str, list[list[Any]]] = {}
    for b in buys:
        if b.quantity > 0:
            queues.setdefault(b.symbol, []).append([float(b.quantity), b.price, b.time_kst, b.name])

    total_pnl = 0.0
    total_cost = 0.0
    matched_any = False
    round_trips: list[RoundTrip] = []

    def _emit(
        symbol: str,
        qty: float,
        buy_price: float | None,
        sell_price: float | None,
        buy_time: str | None,
        sell_time: str | None,
        name: str | None,
        exit_reason: str | None,
    ) -> None:
        pnl: float | None = None
        pnl_pct: float | None = None
        if buy_price is not None and sell_price is not None:
            pnl = (sell_price - buy_price) * qty
            if buy_price != 0:
                pnl_pct = (sell_price / buy_price - 1.0) * 100.0
        round_trips.append(
            RoundTrip(
                symbol=symbol,
                name=name,
                quantity=int(qty),
                buy_price=buy_price,
                sell_price=sell_price,
                buy_time_kst=buy_time,
                sell_time_kst=sell_time,
                holding_minutes=_minutes_between(buy_time, sell_time),
                pnl=pnl,
                pnl_pct=pnl_pct,
                exit_reason=exit_reason,
            )
        )

    def _consume(
        symbol: str,
        qty: int,
        sell_price: float | None,
        sell_time: str | None,
        exit_reason: str | None,
        liq_buy: float | None,
        fallback_name: str | None,
    ) -> None:
        nonlocal total_pnl, total_cost, matched_any
        remaining = float(qty)
        q = queues.get(symbol) or []
        # EOD 청산이 자체 매수단가(브로커 평균원가)를 명시하면 그것을 권위로 삼고
        # FIFO 큐는 재고만 소진 — buys 매핑보다 우선(문서화된 precedence 유지).
        if liq_buy is not None:
            if sell_price is not None:
                total_pnl += (sell_price - liq_buy) * qty
                total_cost += liq_buy * qty
                matched_any = True
            # 왕복 레코드 1건 — 매수 시각/이름은 가능하면 드레인되는 lot 에서 빌려온다.
            lot_time = q[0][2] if q else None
            lot_name = q[0][3] if q else None
            _emit(
                symbol,
                float(qty),
                liq_buy,
                sell_price,
                lot_time,
                sell_time,
                lot_name or fallback_name,
                exit_reason,
            )
            while remaining > 0 and q:  # 재고만 소진(손익 계산 없이)
                lot = q[0]
                lot_qty = lot[0] or 0.0
                take = min(remaining, lot_qty)
                remaining -= take
                lot[0] = lot_qty - take
                if (lot[0] or 0.0) <= 0:
                    q.pop(0)
            return
        while remaining > 0 and q:
            lot = q[0]
            lot_qty = lot[0] or 0.0
            take = min(remaining, lot_qty)
            buy_price = lot[1]
            if buy_price is not None and sell_price is not None:
                total_pnl += (sell_price - buy_price) * take
                total_cost += buy_price * take
                matched_any = True
            _emit(
                symbol,
                take,
                buy_price,
                sell_price,
                lot[2],
                sell_time,
                lot[3] or fallback_name,
                exit_reason,
            )
            remaining -= take
            lot[0] = lot_qty - take
            if (lot[0] or 0.0) <= 0:
                q.pop(0)

    for s in sells:
        if s.quantity > 0:
            _consume(s.symbol, s.quantity, s.price, s.time_kst, s.reason, None, s.name)
    for li in liqs:
        if li.quantity > 0:
            _consume(
                li.symbol,
                li.quantity,
                li.sell_price,
                li.time_kst,
                li.reason or "eod",
                li.buy_price,
                li.name,
            )

    if not matched_any or total_cost == 0:
        return None, None, round_trips
    return total_pnl, total_pnl / total_cost * 100.0, round_trips


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


# 가드 사유 라벨 — 매도 체결 timeline 줄에 붙는 사람용 한국어.
_GUARD_REASON_KO = {
    "partial_take": "부분 익절",
    "stop_loss": "🛑 손절",
    "trailing": "트레일링 청산",
}


def _guard_pct_part(kind: str, raw: dict[str, Any]) -> str:
    """가드 이벤트의 등락률을 ' +2.33%' / ' -2.66%' 형태로. 없으면 빈 문자열.

    partial_take 는 gain_pct(이득, +), stop_loss·trailing 은 drop_pct(하락폭, -)로 라벨링.
    """
    if kind == "partial_take":
        gain = _as_float(raw.get("gain_pct"))
        return f" +{gain:.2f}%" if gain is not None else ""
    drop = _as_float(raw.get("drop_pct"))
    return f" -{drop:.2f}%" if drop is not None else ""


_KILL_SWITCH_REASON_KO = {"daily_loss_kill": "일일 손실 한도"}
_LIQUIDATE_REASON_KO = {
    "liquidate_at_reached": "장 마감",
    "daily_loss_kill": "일일 손실 한도",
}


def _build_timeline(events: LogEvents, names: dict[str, str]) -> list[TimelineEvent]:
    """LogEvents → 시각 순으로 정렬된 한 줄 메시지 리스트.

    포함 이벤트(시각 있는 것만):
    - 발굴 완료 (daytrade.discovery)
    - 매수 체결 (runner.order_simulated / runner.order_accepted) — side=buy
    - 매도 체결 (runner.order_simulated / runner.order_accepted) — side=sell.
      가드 이벤트(손절·부분익절·트레일링)와 (종목, 가격)으로 매칭해 사유를 라벨링.
    - 청산 체결 (runner.liquidate_simulated / runner.liquidate_accepted)
    - kill switch (runner.kill_switch_triggered)
    - 청산 시작 (runner.liquidate_start)

    2026-06-15 회귀: 이전엔 side=buy 매수와 partial_take 가드만 timeline 에 넣어
    손절·트레일링·일반 매도·청산 체결이 통째로 빠졌다(매도 흐름 착시). 이제 매도/청산
    체결을 모두 시각순으로 넣고, 가드 이벤트로 사유를 붙인다. 체결 로그가 따로 없는
    가드 이벤트만 가드 자신의 정보로 폴백 한 줄을 emit.

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

    # 매도 체결을 사유(손절/부분익절/트레일링)로 라벨링하기 위한 가드 색인.
    # (종목, 체결가) → 가드 이벤트 큐. 체결과 1:1 로 소진하고, 남은 가드는 폴백 emit.
    guard_buckets: dict[tuple[str, float | None], list[tuple[str, dict[str, Any]]]] = {}
    for kind, guard_recs in (
        ("partial_take", events.partial_takes),
        ("stop_loss", events.stop_losses),
        ("trailing", events.trailings),
    ):
        for rec in guard_recs:
            symbol = str(rec.get("symbol", ""))
            price = _as_float(rec.get("price"))
            guard_buckets.setdefault((symbol, price), []).append((kind, rec))

    def _add_sell(rec: dict[str, Any], *, simulated: bool) -> None:
        symbol = str(rec.get("symbol", ""))
        qty = _as_int(rec.get("qty")) or 0
        price = _as_float(rec.get("price"))
        if not symbol or qty <= 0:
            return
        label = _with_name_label(symbol, names)
        price_part = f" @{_format_won_int(price)}" if price is not None else ""
        sim_part = " (시뮬)" if simulated else ""
        bucket = guard_buckets.get((symbol, price))
        if bucket:
            # 같은 종목·가격의 가드 이벤트와 매칭 → 사유 라벨 + 등락률.
            kind, guard = bucket.pop(0)
            reason = _GUARD_REASON_KO[kind]
            pct_part = _guard_pct_part(kind, guard)
            _add(
                rec.get("timestamp"),
                kind,
                f"{reason} {label} {qty}주{price_part}{pct_part}{sim_part}",
            )
        else:
            # 가드 없는 일반 매도(시그널·수동 청산 등).
            _add(rec.get("timestamp"), "sell", f"매도 {label} {qty}주{price_part}{sim_part}")

    for rec in events.orders_simulated:
        if str(rec.get("side", "")) == "sell":
            _add_sell(rec, simulated=True)
    for rec in events.orders_accepted:
        if str(rec.get("side", "")) == "sell":
            _add_sell(rec, simulated=False)

    # EOD 강제청산 체결 — orders_* 와 이벤트명이 달라 매도와 겹치지 않는다.
    def _add_liq_fill(rec: dict[str, Any], *, simulated: bool) -> None:
        symbol = str(rec.get("symbol", ""))
        qty = _as_int(rec.get("qty")) or 0
        if not symbol or qty <= 0:
            return
        price = _as_float(rec.get("sell_price"))
        label = _with_name_label(symbol, names)
        price_part = f" @{_format_won_int(price)}" if price is not None else ""
        sim_part = " (시뮬)" if simulated else ""
        _add(rec.get("timestamp"), "liquidate", f"청산 체결 {label} {qty}주{price_part}{sim_part}")

    for rec in events.liquidations_simulated:
        _add_liq_fill(rec, simulated=True)
    for rec in events.liquidations_accepted:
        _add_liq_fill(rec, simulated=False)

    # 체결 로그가 따로 안 남은 가드 이벤트만 폴백으로 emit(가드의 qty/price 사용).
    # partial_take 만 qty_to_sell 을 들고 있어 수량 표기 가능 — 손절/트레일링은 수량 생략.
    for (symbol, price), bucket in guard_buckets.items():
        label = _with_name_label(symbol, names)
        price_part = f" @{_format_won_int(price)}" if price is not None else ""
        for kind, guard in bucket:
            qty = _as_int(guard.get("qty_to_sell")) or 0 if kind == "partial_take" else 0
            qty_part = f" {qty}주" if qty else ""
            reason = _GUARD_REASON_KO[kind]
            pct_part = _guard_pct_part(kind, guard)
            _add(guard.get("timestamp"), kind, f"{reason} {label}{qty_part}{price_part}{pct_part}")

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


def _as_str(raw: object) -> str | None:
    """비어있지 않은 문자열만 통과. 그 외(None·빈문자열·비문자열)는 None."""
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _minutes_between(start_hhmm: str | None, end_hhmm: str | None) -> int | None:
    """ "HH:MM" 두 시각의 분 차이(보유시간). 같은 날 가정 — 음수면 None.

    체결 시각은 모두 같은 거래일이라 단순 분 차이로 충분. 파싱 실패·결손 시 None.
    """
    if not start_hhmm or not end_hhmm:
        return None
    try:
        sh, sm = (int(x) for x in start_hhmm.split(":"))
        eh, em = (int(x) for x in end_hhmm.split(":"))
    except ValueError, AttributeError:
        return None
    diff = (eh * 60 + em) - (sh * 60 + sm)
    return diff if diff >= 0 else None


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
    round_trips_raw = payload.get("round_trips") or []
    round_trips = [RoundTrip(**rt) for rt in round_trips_raw if isinstance(rt, dict)]
    blocked_outcomes_raw = payload.get("blocked_outcomes") or []
    blocked_outcomes = [BlockedOutcome(**b) for b in blocked_outcomes_raw if isinstance(b, dict)]
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
        # 라운드트립 유실 복원(2026-06-10) — 저장은 asdict 로 전 필드가 나가는데
        # 복원에서 빠져 있어 turnover_ratio(과매매 진단 핵심)가 주간 인사이트·
        # 대시보드에서 항상 None 으로 떨어졌다.
        traded_notional=payload.get("traded_notional"),
        turnover_ratio=payload.get("turnover_ratio"),
        orders_failed_count=int(payload.get("orders_failed_count") or 0),
        liquidations_failed_count=int(payload.get("liquidations_failed_count") or 0),
        balance_failure_count=int(payload.get("balance_failure_count") or 0),
        fatal_count=int(payload.get("fatal_count") or 0),
        # 2026-06-15 신규 — 직렬화 footgun 회귀 방지: asdict 는 자동이지만 복원은 수동.
        regime=payload.get("regime"),
        breadth_advancing_pct=payload.get("breadth_advancing_pct"),
        breadth_mean_change_pct=payload.get("breadth_mean_change_pct"),
        breadth_passed=payload.get("breadth_passed"),
        round_trips=round_trips,
        blocked_outcomes=blocked_outcomes,
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
    "BlockedOutcome",
    "ClosingHolding",
    "DailyReport",
    "LiquidationRecord",
    "MarketGateInfo",
    "RoundTrip",
    "TimelineEvent",
    "TradeRecord",
    "build_daily_report",
    "load_daily_report",
    "merge_symbol_names",
    "save_daily_report",
    "with_symbol_name",
]
