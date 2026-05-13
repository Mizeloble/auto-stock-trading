"""백테스트용 시뮬레이션 브로커.

실제 체결의 단순화된 모델:
- 지정가/시장가 구분 없이 **다음 봉 시가**에 체결 (look-ahead 방지)
- 수수료: 매수/매도 각 0.015% (실제 증권사 평균 수준)
- 거래세: **매도 시** 0.20% (KOSPI/KOSDAQ. 2026년 인상 반영, 이전 0.18%)
- 슬리피지:
  - ``slippage_model="fixed"`` (기본): ``slippage_bps`` 고정. 대형주·소량 주문에 적합.
  - ``slippage_model="impact"``: 주문량 / 봉 거래량 비율에 비례 가산 (29단계 PR35, #4).
    소형주·매수 폭주 시 백테스트와 실거래 PnL 괴리를 줄임. ``bar_volume`` 인자 필요.

변경 시 `__init__` 파라미터 기본값을 조정한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..strategy.base import Side


@dataclass
class Trade:
    symbol: str
    side: Side
    ts: datetime
    price: float
    quantity: int
    fee: float
    tax: float

    @property
    def cash_flow(self) -> float:
        """현금 흐름 — 매수는 음수, 매도는 양수."""
        gross = self.price * self.quantity
        if self.side is Side.BUY:
            return -(gross + self.fee)
        return gross - self.fee - self.tax


@dataclass
class Position:
    quantity: int = 0
    avg_price: float = 0.0

    def apply(self, side: Side, price: float, qty: int) -> None:
        if side is Side.BUY:
            total = self.avg_price * self.quantity + price * qty
            self.quantity += qty
            self.avg_price = total / self.quantity if self.quantity else 0.0
        else:
            self.quantity = max(0, self.quantity - qty)
            if self.quantity == 0:
                self.avg_price = 0.0


@dataclass
class SimulatedBroker:
    cash: float
    commission_rate: float = 0.00015  # 0.015%
    tax_rate_sell: float = 0.0020  # 0.20% 매도 시 (2026년 인상 반영)
    slippage_bps: float = 5.0  # 1bp = 0.01% — 5bp = 0.05% 시장가 체결 보수적 기본
    # 29단계 PR35 (#4) — 시장 충격 슬리피지 모델.
    # "fixed": slippage_bps 고정 (기본, 기존 동작).
    # "impact": fixed + impact_coefficient * (수량/봉_거래량 * 100) bp.
    #   예: 비율 1% + impact_coefficient=2 → +2bp. 비율 5% → +10bp. 10% → +20bp.
    #   bar_volume=0 또는 미주입이면 fixed 로 fallback (안전).
    slippage_model: str = "fixed"
    impact_coefficient: float = 2.0
    positions: dict[str, Position] = field(default_factory=dict)
    trades: list[Trade] = field(default_factory=list)

    def _effective_slippage_bps(self, quantity: int, bar_volume: int) -> float:
        """주문 수량 + 봉 거래량 → 실효 슬리피지 bp.

        slippage_model="fixed" → slippage_bps 그대로.
        slippage_model="impact" → fixed + impact_coefficient * (수량/봉_거래량) * 100.
        bar_volume<=0 (데이터 누락) → fixed 로 fallback.
        """
        if self.slippage_model != "impact" or bar_volume <= 0 or quantity <= 0:
            return self.slippage_bps
        ratio_pct = (quantity / bar_volume) * 100.0
        return self.slippage_bps + self.impact_coefficient * ratio_pct

    def _apply_slippage(
        self, price: float, side: Side, *, quantity: int = 0, bar_volume: int = 0
    ) -> float:
        bps = self._effective_slippage_bps(quantity, bar_volume)
        slip = price * (bps / 10_000.0)
        return price + slip if side is Side.BUY else price - slip

    def execute(
        self,
        *,
        symbol: str,
        side: Side,
        quantity: int,
        bar_open: float,
        ts: datetime,
        bar_volume: int = 0,
    ) -> Trade | None:
        if quantity <= 0:
            return None
        price = self._apply_slippage(bar_open, side, quantity=quantity, bar_volume=bar_volume)
        gross = price * quantity
        fee = gross * self.commission_rate
        tax = gross * self.tax_rate_sell if side is Side.SELL else 0.0

        pos = self.positions.setdefault(symbol, Position())
        if side is Side.BUY:
            cost = gross + fee
            if cost > self.cash:
                # 잔고 부족 — 가능한 수량까지 축소
                max_qty = int((self.cash) // (price * (1 + self.commission_rate)))
                if max_qty <= 0:
                    return None
                quantity = max_qty
                gross = price * quantity
                fee = gross * self.commission_rate
                cost = gross + fee
            self.cash -= cost
        else:
            if pos.quantity < quantity:
                quantity = pos.quantity
                if quantity <= 0:
                    return None
                gross = price * quantity
                fee = gross * self.commission_rate
                tax = gross * self.tax_rate_sell
            self.cash += gross - fee - tax

        pos.apply(side, price, quantity)
        trade = Trade(
            symbol=symbol, side=side, ts=ts, price=price, quantity=quantity, fee=fee, tax=tax
        )
        self.trades.append(trade)
        return trade

    def equity(self, prices: dict[str, float]) -> float:
        holdings = sum(
            pos.quantity * prices.get(sym, pos.avg_price) for sym, pos in self.positions.items()
        )
        return self.cash + holdings
