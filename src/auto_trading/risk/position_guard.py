"""PositionGuard — 종목별 stop-loss / 트레일링 스톱 / 부분 익절 결정.

전략 `on_bar` 가 진입(BUY) 만 담당하는 동안, 러너가 매 봉 끝에 PositionGuard.check 를
호출해 보유 포지션을 모니터링한다. 결정은 다음 우선순위로 한 번에 1건만 반환:

1. **stop-loss** — 매수가 대비 `-stop_loss_pct` 이하 도달 → 전량 청산.
2. **trailing stop** — 보유 중 고점 대비 `-trailing_stop_pct` 이하 도달 → 잔량 전체.
3. **partial take** — 매수가 대비 `+partial_take_pct` 이상 도달 + 아직 부분 익절 X →
   `qty * partial_take_qty_ratio` 만 매도, 잔량은 트레일링 스톱이 추적.

기본값(0 또는 None) 이면 해당 규칙 비활성. 모두 비활성이면 PositionGuard 는 항상 None
반환 — 기존 paper-day 동작과 호환.

도메인 경계: PositionGuard 는 입력 (symbol, price, qty, avg_price) → 출력
(GuardAction | None) 만 다루고, 외부 호출(REST/WS) 도 alpha state 변경(account.positions)
도 하지 않는다. 결정 후 실제 주문 송신·상태 갱신은 러너 책임.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

import structlog

from .manager import RiskLimits

log = structlog.get_logger(__name__)


class GuardReason(StrEnum):
    STOP_LOSS = "stop_loss"
    TRAILING = "trailing"
    PARTIAL_TAKE = "partial_take"


@dataclass(frozen=True)
class GuardAction:
    """guard 가 발화한 매도 결정. 러너가 그대로 SELL 주문으로 변환."""

    symbol: str
    quantity: int
    reason: GuardReason
    price: float  # 결정 시점 가격(로그·디버그용. 시장가 주문이라 체결가는 별도)


@dataclass
class _Tracking:
    """종목별 내부 상태. 평단가는 러너가 매 check 마다 주입(키움 갱신 가능성)."""

    avg_price: float
    highest_seen: float
    partial_taken: bool = False


@dataclass
class PositionGuard:
    """RiskLimits 의 stop/trailing/partial 파라미터를 입력 → guard 결정.

    무엇/왜:
    - 단일 인스턴스가 여러 종목 추적. 종목별 상태는 내부 dict.
    - 매 check 호출에서 highest_seen 을 갱신해 트레일링 기준점 유지.
    - 동일 종목이 청산 후 재진입(다회 진입) 하면 reset(symbol) 로 상태 초기화 필요.

    어떻게 확인: tests/test_position_guard.py 가 4개 발화 시나리오 + 비활성 무동작 검증.
    """

    limits: RiskLimits
    _tracking: dict[str, _Tracking] = field(default_factory=dict)

    @property
    def is_active(self) -> bool:
        """3개 규칙 중 하나라도 활성이면 True. 모두 비활성이면 check 가 항상 None."""
        return (
            self.limits.stop_loss_pct > 0
            or self.limits.trailing_stop_pct > 0
            or self.limits.partial_take_pct > 0
        )

    def reset(self, symbol: str | None = None) -> None:
        """`symbol=None` 이면 전체, 아니면 해당 종목만 초기화. 다회 진입 사이의 청산 후 호출."""
        if symbol is None:
            self._tracking.clear()
        else:
            self._tracking.pop(symbol, None)

    def check(
        self,
        *,
        symbol: str,
        price: float,
        quantity: int,
        avg_price: float,
    ) -> GuardAction | None:
        """매 봉 끝에 호출. 보유 0이면 자동으로 추적 해제 후 None 반환."""
        if quantity <= 0 or price <= 0 or avg_price <= 0:
            self._tracking.pop(symbol, None)
            return None
        if not self.is_active:
            return None

        track = self._tracking.get(symbol)
        if track is None:
            track = _Tracking(avg_price=avg_price, highest_seen=price)
            self._tracking[symbol] = track
        else:
            # 평단가는 매수 추가 시 변할 수 있으니 항상 최신값으로.
            track.avg_price = avg_price
            if price > track.highest_seen:
                track.highest_seen = price

        # 1) stop-loss — 매수가 대비 하락폭이 한도 도달.
        if self.limits.stop_loss_pct > 0:
            drop = 1.0 - price / track.avg_price
            if drop >= self.limits.stop_loss_pct:
                log.info(
                    "position_guard.stop_loss",
                    symbol=symbol,
                    price=price,
                    avg_price=track.avg_price,
                    drop_pct=round(drop * 100, 3),
                )
                self._tracking.pop(symbol, None)
                return GuardAction(symbol, quantity, GuardReason.STOP_LOSS, price)

        # 2) 트레일링 스톱 — 고점 대비 하락폭이 한도 도달.
        if self.limits.trailing_stop_pct > 0 and track.highest_seen > 0:
            trail_drop = 1.0 - price / track.highest_seen
            if trail_drop >= self.limits.trailing_stop_pct:
                log.info(
                    "position_guard.trailing",
                    symbol=symbol,
                    price=price,
                    highest=track.highest_seen,
                    drop_pct=round(trail_drop * 100, 3),
                )
                self._tracking.pop(symbol, None)
                return GuardAction(symbol, quantity, GuardReason.TRAILING, price)

        # 3) 부분 익절 — 매수가 대비 상승폭 한도 도달 + 아직 안 했음.
        if self.limits.partial_take_pct > 0 and not track.partial_taken:
            gain = price / track.avg_price - 1.0
            if gain >= self.limits.partial_take_pct:
                ratio = max(0.0, min(1.0, self.limits.partial_take_qty_ratio))
                if ratio <= 0:
                    return None  # 잘못된 설정 (ratio=0) 은 발화 X.
                qty_to_sell = max(1, int(quantity * ratio))
                qty_to_sell = min(qty_to_sell, quantity - 1) if ratio < 1.0 else quantity
                if qty_to_sell <= 0:
                    return None
                track.partial_taken = True
                log.info(
                    "position_guard.partial_take",
                    symbol=symbol,
                    price=price,
                    avg_price=track.avg_price,
                    gain_pct=round(gain * 100, 3),
                    qty_to_sell=qty_to_sell,
                    qty_remaining=quantity - qty_to_sell,
                )
                return GuardAction(symbol, qty_to_sell, GuardReason.PARTIAL_TAKE, price)

        return None


__all__ = ["GuardAction", "GuardReason", "PositionGuard"]
