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

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

import structlog

from .._common import Side
from ..account.orders import Order, OrderType
from ..strategy.base import Signal
from .correlation import max_correlation_with_holdings

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

    deposit: float  # 사용 가능 현금
    total_equity: float  # 예수금 + 평가금
    positions: dict[str, int] = field(default_factory=dict)  # symbol -> 수량
    avg_prices: dict[str, float] = field(default_factory=dict)  # symbol -> 평단가
    day_start_equity: float = 0.0  # 당일 시작 에쿼티 (손실 한도 계산용)
    # 23단계 Phase B — 일일 누적 매수대금. 매수 체결·시뮬마다 러너가 누적, 잔고 갱신 시
    # 보존(이전 값 그대로 carry-over). 자정 리셋은 러너 재시작이 자연스럽게 처리.
    today_notional_used: float = 0.0


@dataclass
class RiskLimits:
    max_position_weight: float = 0.20  # 한 종목당 예수금 대비 20%
    max_portfolio_exposure: float = 0.80  # 현금+주식 중 주식 80% 한도
    daily_loss_limit: float = 0.03  # 당일 -3% 시 신규 진입 차단
    min_order_value: float = 10_000.0  # 너무 작은 주문 차단 (수수료 손해)
    # 23단계 Phase B — 보수적 기본은 비활성(0/False). 활성은 명시적 설정 필요.
    max_order_value: float = 0.0  # 1회 주문 절대 한도(원). 0 이면 무제한
    daily_notional_limit: float = 0.0  # 일일 총 매수대금 한도(원). 0 이면 무제한
    daily_loss_kill: bool = False  # True 면 daily_loss 도달 시 보유 강제 청산 + 정지
    # 23단계 Phase C — PositionGuard 파라미터. 모두 0 이면 비활성.
    stop_loss_pct: float = 0.0  # 매수가 대비 -X% 도달 시 전량 청산. 예: 0.01 = -1%
    trailing_stop_pct: float = 0.0  # 고점 대비 -X% 도달 시 잔량 청산. 예: 0.005 = -0.5%
    partial_take_pct: float = 0.0  # 매수가 대비 +X% 도달 시 부분 익절. 예: 0.015 = +1.5%
    partial_take_qty_ratio: float = 0.5  # 부분 익절 비율 — 0.5 면 절반만 매도
    # 29단계 PR9 (#14) — 연속 손절 자동 일시중지. 0 이면 비활성.
    # 활성: max_consecutive_losses=3 → 3연속 손절 시 24h 신규 진입 차단.
    # 운영자가 텔레그램에서 수동 unpause 가능 (PR10 — runner 통합).
    max_consecutive_losses: int = 0
    consecutive_loss_pause_sec: int = 86_400  # 24h
    # 29단계 PR27 (#15 Phase 1) — 보유 종목 간 페어 상관계수 가드.
    # 0 이면 비활성. 활성 권장: 0.7 (그 이상이면 사실상 같은 종목 — 분산 효과 0).
    # 러너가 30일 일별 수익률 → pairwise_pearson_correlation 으로 미리 계산해
    # evaluate(holding_correlations=...) 로 주입. 비활성 시 evaluate 동작 불변.
    max_position_correlation: float = 0.0
    # 회전율 가드 — 종목당 하루 BUY 허용 횟수. 0 이면 비활성.
    # 한도 초과 시 BUY 시그널만 차단 (SELL/청산은 영향 없음). 카운터·리셋은 러너 책임.
    max_buys_per_symbol: int = 0
    # 29단계 PR39 (#6) — ATR 기반 weight 정규화.
    # False: signal.weight 그대로. True: ATR 데이터 있을 때 weight x (target / atr_pct).
    # 변동성 큰 종목엔 작은 weight, 작은 종목엔 큰 weight (0~1 clamp).
    # atr_target_risk_pct = 0.01 = "1 ATR 변동 ≈ 자본 1% 손실" 기준.
    atr_normalized_weight: bool = False
    atr_target_risk_pct: float = 0.01


@dataclass
class RiskManager:
    limits: RiskLimits = field(default_factory=RiskLimits)

    def evaluate(
        self,
        signal: Signal,
        *,
        price: float,
        account: AccountState,
        holding_correlations: Mapping[tuple[str, str], float] | None = None,
        atr_per_symbol: Mapping[str, float] | None = None,
    ) -> RiskDecision:
        """Signal → 검증된 Order 또는 거절.

        holding_correlations:
            러너가 미리 계산한 보유 종목 간 페어 상관계수. 키는 정렬된 튜플.
            ``max_position_correlation > 0`` 이고 이 인자가 제공되면 BUY 시 상관 가드 적용.
            None 이거나 한도 비활성이면 가드 스킵 — 기존 동작과 동일.

        atr_per_symbol (29단계 PR39 #6):
            symbol → ATR 값 (절대 가격 단위). ``atr_normalized_weight=True`` + 데이터 있으면
            BUY weight 를 ATR 역수로 정규화 (변동성 큰 종목 비중 ↓). 비활성·없음 → 기존 동작.
        """
        if price <= 0:
            return RiskDecision(Decision.REJECT, reason="invalid price")

        # 돈·수량 산식은 float 반올림 오차를 피하기 위해 Decimal 로 환산한다.
        price_d = Decimal(str(price))
        weight = max(Decimal(0), min(Decimal(1), Decimal(str(signal.weight))))

        # 29단계 PR39 (#6) — ATR 기반 weight 정규화 (BUY 시점에만 의미. SELL 은 보유 비례).
        if (
            signal.side is Side.BUY
            and self.limits.atr_normalized_weight
            and atr_per_symbol is not None
        ):
            atr_val = atr_per_symbol.get(signal.symbol)
            if atr_val is not None and atr_val > 0 and price > 0:
                atr_pct = atr_val / price
                target_pct = self.limits.atr_target_risk_pct
                if atr_pct > 0 and target_pct > 0:
                    ratio = Decimal(str(target_pct)) / Decimal(str(atr_pct))
                    weight = max(Decimal(0), min(Decimal(1), weight * ratio))

        if signal.side is Side.SELL:
            held = account.positions.get(signal.symbol, 0)
            if held <= 0:
                return RiskDecision(Decision.REJECT, reason="no position to sell")
            qty = max(1, int((Decimal(held) * weight).to_integral_value(rounding="ROUND_HALF_UP")))
            qty = min(qty, held)
            order = Order(
                symbol=signal.symbol,
                side=Side.SELL,
                quantity=qty,
                order_type=OrderType.MARKET,
                price=price_d if signal.price_hint is None else signal.price_hint,
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

        # 1b) 29단계 PR27 — 상관계수 가드. 한도 > 0 + 페어 데이터가 있을 때만.
        corr_limit = float(self.limits.max_position_correlation)
        if corr_limit > 0 and holding_correlations is not None:
            existing = [s for s, q in account.positions.items() if q > 0 and s != signal.symbol]
            best = max_correlation_with_holdings(signal.symbol, existing, holding_correlations)
            if best is not None and best[1] >= corr_limit:
                reason = (
                    f"correlation guard ({signal.symbol} vs {best[0]} "
                    f"corr={best[1]:.2f} >= {corr_limit:.2f})"
                )
                log.info("risk.reject", reason=reason, symbol=signal.symbol)
                return RiskDecision(Decision.REJECT, reason=reason)

        equity_d = Decimal(str(account.total_equity))
        deposit_d = Decimal(str(account.deposit))
        pos_weight = Decimal(str(self.limits.max_position_weight))
        exposure = Decimal(str(self.limits.max_portfolio_exposure))
        min_order = Decimal(str(self.limits.min_order_value))

        # 2) 포지션 비중
        portfolio_budget = equity_d * pos_weight
        existing_value = Decimal(account.positions.get(signal.symbol, 0)) * Decimal(
            str(account.avg_prices.get(signal.symbol, price))
        )
        remaining_budget = max(Decimal(0), portfolio_budget - existing_value)

        # 3) 포트폴리오 전체 익스포저
        held_value = sum(
            (
                Decimal(q) * Decimal(str(account.avg_prices.get(s, 0.0)))
                for s, q in account.positions.items()
            ),
            start=Decimal(0),
        )
        exposure_cap = equity_d * exposure - held_value
        budget = min(remaining_budget, exposure_cap, deposit_d)
        budget *= weight

        # 4) 23단계 Phase B — 1회 주문 절대 한도 (max_order_value > 0 일 때만 적용)
        max_order = Decimal(str(self.limits.max_order_value))
        if max_order > 0 and budget > max_order:
            budget = max_order

        # 5) 23단계 Phase B — 일일 누적 매수대금 한도
        daily_cap = Decimal(str(self.limits.daily_notional_limit))
        if daily_cap > 0:
            used = Decimal(str(account.today_notional_used))
            remaining = max(Decimal(0), daily_cap - used)
            if remaining <= 0:
                reason = (
                    f"daily notional limit reached ({float(used):,.0f}/{float(daily_cap):,.0f})"
                )
                log.info("risk.reject", reason=reason, symbol=signal.symbol)
                return RiskDecision(Decision.REJECT, reason=reason)
            if budget > remaining:
                budget = remaining

        if budget < min_order:
            reason = f"budget too small ({int(budget)})"
            log.info("risk.reject", reason=reason, symbol=signal.symbol)
            return RiskDecision(Decision.REJECT, reason=reason)

        qty = int(budget // price_d)
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

    def should_force_liquidate(self, account: AccountState) -> bool:
        """일일 손실 한도 도달 + `daily_loss_kill` 활성 시 True (kill switch).

        무엇/왜:
        - `evaluate()` 의 daily_loss 게이트는 신규 BUY 만 차단 — 보유분은 그대로 들고감.
        - kill switch 는 True 가 되면 러너가 `liquidate_all + request_stop` 으로 즉시 정리.
        - 기본 비활성. 사용자가 `daily_loss_kill=True` 명시적으로 켜야 동작.

        어떻게 확인: tests/test_risk_manager.py 의 kill switch 케이스에서 검증.
        """
        if not self.limits.daily_loss_kill:
            return False
        if account.day_start_equity <= 0:
            return False
        today_change = account.total_equity / account.day_start_equity - 1.0
        return today_change <= -abs(self.limits.daily_loss_limit)

    def kill_switch_baseline_missing(self, account: AccountState) -> bool:
        """kill switch 가 켜졌는데 판정 기준(day_start_equity)이 없어 *무방비* 인 상태.

        무엇/왜:
        - `should_force_liquidate` 는 day_start_equity<=0 이면 조용히 False 를 돌려준다
          (fail-open). 그 자체로는 안전(잘못 청산 안 함)하지만, 사용자는 "안전 브레이크가
          지금 작동 불능" 이라는 사실을 알 길이 없다 → 러너가 이 상태를 *한 번* 경고하도록
          분리한 판정용 순수 함수.
        - daily_loss_kill 이 꺼져 있으면 무방비도 아니므로 False.

        어떻게 확인: tests/test_risk_manager.py 의 baseline-missing 케이스.
        """
        return self.limits.daily_loss_kill and account.day_start_equity <= 0
