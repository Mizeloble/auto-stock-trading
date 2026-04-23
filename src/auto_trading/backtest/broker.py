"""백테스트용 시뮬레이션 브로커.

실제 체결의 단순화된 모델:
- 지정가/시장가 구분 없이 **다음 봉 시가**에 체결 (look-ahead 방지)
- 수수료: 매수/매도 각 0.015% (실제 증권사 평균 수준)
- 거래세: **매도 시** 0.18% (KOSPI/KOSDAQ 평균. 2025년 기준 값이며 변동 가능)
- 슬리피지: 기본 0 bp. 필요 시 외부에서 주입.

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
    commission_rate: float = 0.00015   # 0.015%
    tax_rate_sell: float = 0.0018      # 0.18% 매도 시
    slippage_bps: float = 0.0          # 1bp = 0.01%
    positions: dict[str, Position] = field(default_factory=dict)
    trades: list[Trade] = field(default_factory=list)

    def _apply_slippage(self, price: float, side: Side) -> float:
        slip = price * (self.slippage_bps / 10_000.0)
        return price + slip if side is Side.BUY else price - slip

    def execute(self, *, symbol: str, side: Side, quantity: int, bar_open: float, ts: datetime) -> Trade | None:
        if quantity <= 0:
            return None
        price = self._apply_slippage(bar_open, side)
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
        trade = Trade(symbol=symbol, side=side, ts=ts, price=price, quantity=quantity, fee=fee, tax=tax)
        self.trades.append(trade)
        return trade

    def equity(self, prices: dict[str, float]) -> float:
        holdings = sum(pos.quantity * prices.get(sym, pos.avg_price) for sym, pos in self.positions.items())
        return self.cash + holdings
