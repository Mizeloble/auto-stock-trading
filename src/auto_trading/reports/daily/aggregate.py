"""수치 집계 — 비용 추정·거래대금·FIFO 라운드트립 실현손익.

매수·매도·청산 레코드를 받아 결정론적으로 합산한다. 외부 호출 없음.
"""

from __future__ import annotations

from typing import Any

from ._util import _minutes_between
from .models import LiquidationRecord, RoundTrip, TradeRecord

# 비용 추정 상수 — backtest/broker.py 의 기본값과 일치시킨다 (단일 진실 출처).
# 두 모듈을 import 로 묶지 않는 이유: reports/ 는 외부 호출 금지(CLAUDE.md), 의존 최소화.
# broker.py 가 변경되면 여기 값도 같이 갱신할 것.
COST_COMMISSION_RATE = 0.00015  # 0.015% 매수+매도
COST_TAX_RATE_SELL = 0.0020  # 0.20% 매도 시 (2026 인상)
COST_SLIPPAGE_BPS = 5.0  # 0.05% 매수+매도


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
