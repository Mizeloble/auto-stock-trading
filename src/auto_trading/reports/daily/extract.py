"""로그·스냅샷 → 도메인 레코드 추출/파생.

로그 dict(LogEvents) 와 topcap 스냅샷(TopcapSnapshots) 을 받아 매수·매도·청산·
게이트·차단 종목 레코드를 만들고, 스냅샷 조인으로 진입 피처·MFE/MAE·기회비용을
보강한다. 모두 결정론적이며 외부 호출은 없다.
"""

from __future__ import annotations

from dataclasses import replace

from ...notify.state import RunnerStateSnapshot
from ..log_parser import LogEvents
from ..snapshot_reader import TopcapSnapshots
from ._util import _as_float, _as_int, _as_str, _ts_to_kst_hhmm
from .models import (
    BlockedOutcome,
    ClosingHolding,
    LiquidationRecord,
    MarketGateInfo,
    RoundTrip,
    TradeRecord,
)


def _is_buying_power_reject(reason: str) -> bool:
    """매수여력 부족 계열 거부인지 — 일 매수대금 한도 소진/예산 부족/수량0.

    runner.signal_rejected 의 reason 문자열로 판별. 상관·일손실·거래불가 등 다른
    거부는 제외(이건 "재진입을 한도가 막았나" 진단용이라 매수여력 계열만 센다).
    """
    return (
        reason.startswith("budget too small")
        or reason == "quantity 0 after sizing"
        or reason.startswith("daily notional limit reached")
    )


def _derive_no_trade_reason(
    events: LogEvents,
    buys: list[TradeRecord],
    discovery_chosen: list[str],
    discovery_blocked: list[tuple[str, str]],
    breadth_passed: bool | None,
) -> str | None:
    """매수 0건인 날의 사유를 초보 친화 한 줄로 분류. 매수가 있으면 None.

    "2주 진입 0" 같은 사건을 리포트만 보고 바로 진단할 수 있게 한다. 신규 데이터
    없이 기존 events 파생. 우선순위: breadth 차단 → 전 후보 게이트 차단 → 발굴 없음
    → 일일 손실 한도 중단 → 발굴됐으나 체결 0.
    """
    if buys:
        return None
    if breadth_passed is False:
        return "시장 폭 게이트 차단 — 상승 종목이 부족해 오늘은 진입하지 않았어요."
    if not discovery_chosen and discovery_blocked:
        return "발굴 후보가 모두 지수 게이트에 막혀 진입하지 않았어요."
    if events.discovery is None and events.rank is None:
        return "발굴 기록이 없어요 — 거래일이 아니거나 로그가 없는 날이에요."
    if events.kill_switches:
        return "일일 손실 한도에 도달해 매매를 중단했어요."
    if discovery_chosen:
        return "종목은 발굴됐지만 매수 신호가 없거나 진입이 막혀 체결 0건이에요."
    return "오늘은 매수가 없었어요."


def _enrich_buys(buys: list[TradeRecord], snaps: TopcapSnapshots) -> list[TradeRecord]:
    """매수 레코드에 진입 시점 등락률·순위(스냅샷 조인)를 채운다. 결손은 None 유지."""
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
