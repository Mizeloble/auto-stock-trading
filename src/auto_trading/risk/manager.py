"""리스크 매니저.

Strategy가 내놓은 Signal 을 실제 주문(Order) 로 바꾸기 전 검증·사이징하는 계층.

역할:
1. 종목당 최대 비중 한도 (예: 예수금 20%)
2. 포트폴리오 전체 최대 익스포저 (예: 80%)
3. 일일 손실 한도 (-3% 도달 시 신규 진입 차단)
4. Signal.weight 를 실제 수량으로 환산

기본값은 **보수적**. 완화는 사용자 명시 승인 후에만.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

import structlog

from .._common import Side
from ..account.orders import Order, OrderType
from ..strategy.base import Signal

log = structlog.get_logger(__name__)


class Decision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


@dataclass(frozen=True)
class RiskDecision:
    decision: Decision
    order: Order | None = None
    reason: str = ""


@dataclass
class AccountState:
    """러너가 매 사이클 갱신하는 계좌 요약."""

    deposit: float                             # 사용 가능 현금
    total_equity: float                        # 예수금 + 평가금
    positions: dict[str, int] = field(default_factory=dict)   # symbol -> 수량
    avg_prices: dict[str, float] = field(default_factory=dict)  # symbol -> 평단가
    day_start_equity: float = 0.0              # 당일 시작 에쿼티 (손실 한도 계산용)


@dataclass
class RiskLimits:
    max_position_weight: float = 0.20          # 한 종목당 예수금 대비 20%
    max_portfolio_exposure: float = 0.80       # 현금+주식 중 주식 80% 한도
    daily_loss_limit: float = 0.03             # 당일 -3% 시 신규 진입 차단
    min_order_value: float = 10_000.0          # 너무 작은 주문 차단 (수수료 손해)


@dataclass
class RiskManager:
    limits: RiskLimits = field(default_factory=RiskLimits)

    def evaluate(
        self,
        signal: Signal,
        *,
        price: float,
        account: AccountState,
    ) -> RiskDecision:
        if price <= 0:
            return RiskDecision(Decision.REJECT, reason="invalid price")

        if signal.side is Side.SELL:
            held = account.positions.get(signal.symbol, 0)
            if held <= 0:
                return RiskDecision(Decision.REJECT, reason="no position to sell")
            qty = max(1, int(round(held * max(0.0, min(1.0, signal.weight)))))
            qty = min(qty, held)
            order = Order(
                symbol=signal.symbol,
                side=Side.SELL,
                quantity=qty,
                order_type=OrderType.MARKET,
                price=Decimal(str(price)) if signal.price_hint is None else signal.price_hint,
            )
            return RiskDecision(Decision.APPROVE, order=order, reason="sell approved")

        # --- BUY 시나리오 가드 ---

        # 1) 일일 손실 한도
        if account.day_start_equity > 0:
            today_change = account.total_equity / account.day_start_equity - 1.0
            if today_change <= -abs(self.limits.daily_loss_limit):
                reason = f"daily loss limit hit ({today_change:.2%})"
                log.warning("risk.reject", reason=reason, symbol=signal.symbol)
                return RiskDecision(Decision.REJECT, reason=reason)

        # 2) 포지션 비중
        portfolio_budget = account.total_equity * self.limits.max_position_weight
        existing_value = (
            account.positions.get(signal.symbol, 0)
            * account.avg_prices.get(signal.symbol, price)
        )
        remaining_budget = max(0.0, portfolio_budget - existing_value)

        # 3) 포트폴리오 전체 익스포저
        held_value = sum(
            q * account.avg_prices.get(s, 0.0) for s, q in account.positions.items()
        )
        exposure_cap = account.total_equity * self.limits.max_portfolio_exposure - held_value
        budget = min(remaining_budget, exposure_cap, account.deposit)
        budget *= max(0.0, min(1.0, signal.weight))

        if budget < self.limits.min_order_value:
            reason = f"budget too small ({budget:.0f})"
            log.info("risk.reject", reason=reason, symbol=signal.symbol)
            return RiskDecision(Decision.REJECT, reason=reason)

        qty = int(budget // price)
        if qty <= 0:
            return RiskDecision(Decision.REJECT, reason="quantity 0 after sizing")

        order = Order(
            symbol=signal.symbol,
            side=Side.BUY,
            quantity=qty,
            order_type=OrderType.MARKET,
            price=signal.price_hint,
        )
        return RiskDecision(Decision.APPROVE, order=order, reason="buy approved")
