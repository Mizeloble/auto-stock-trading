"""주문(매수/매도/정정/취소) 래퍼.

모의투자도 동일 엔드포인트를 사용. 실거래 전환은 `KIWOOM_MODE=real` 로 해야만 가능.
러너가 리스크 매니저 통과한 Order 객체만 여기로 넘긴다.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog

from .._common import Side, normalize_symbol
from ..kiwoom.endpoints import PATH_ORDER, TR
from ..kiwoom.rest import KiwoomRestClient, is_outcome_unknown

if TYPE_CHECKING:
    from .order_journal import OrderJournal

log = structlog.get_logger(__name__)

__all__ = ["Order", "OrderResult", "OrderType", "Side", "cancel_order", "place_order"]


class OrderType(StrEnum):
    MARKET = "market"  # 시장가
    LIMIT = "limit"  # 지정가


@dataclass(frozen=True)
class Order:
    symbol: str
    side: Side
    quantity: int
    order_type: OrderType = OrderType.MARKET
    price: Decimal | None = None  # limit 일 때만
    exchange: str = "KRX"


@dataclass(frozen=True)
class OrderResult:
    order_no: str
    raw: dict
    # 29단계 PR1 — 호출자가 부여하거나 자동 생성된 client UUID. 봇 재시작 후
    # 키움 OrderID(order_no) 와 매칭하기 위한 키. 빈 문자열은 journal 미사용.
    client_id: str = ""


async def place_order(
    client: KiwoomRestClient,
    order: Order,
    *,
    journal: OrderJournal | None = None,
    client_id: str | None = None,
) -> OrderResult:
    """매수/매도 주문 전송.

    키움 주문 파라미터명:
    - `dmst_stex_tp`: 국내거래소 구분 (KRX/NXT)
    - `stk_cd`: 종목코드
    - `ord_qty`: 주문수량
    - `ord_uv`: 주문단가 (시장가는 "")
    - `trde_tp`: 매매구분 (0 지정가, 3 시장가 등)

    29단계 PR1 — `journal` 이 주어지면 송신 직전 ``submit`` 이벤트를, 응답
    수신 직후 ``accepted`` 이벤트를 기록한다. 송신 중 예외가 나면 ``failed``
    를 기록한 뒤 예외를 그대로 전파 (호출자 동작 보존). `client_id` 미지정
    시 자동 UUID. 명시 지정은 *재시도* 시 동일 UUID 로 묶기 위함.
    """
    trde_tp = "3" if order.order_type is OrderType.MARKET else "0"
    price_str = "" if order.order_type is OrderType.MARKET else str(order.price or "")

    # 방어선: 잔고/feed normalize 가 빠진 경로에서 'A005930' 같은 변종이 흘러들어와도
    # 송신 직전에 6자리 숫자로. 2026-05-08 청산 거절 회귀.
    symbol = normalize_symbol(order.symbol)

    if client_id is None:
        from .order_journal import new_client_id

        client_id = new_client_id()
    if journal is not None:
        journal.submit(client_id=client_id, order=order)

    body = {
        "dmst_stex_tp": order.exchange,
        "stk_cd": symbol,
        "ord_qty": str(order.quantity),
        "ord_uv": price_str,
        "trde_tp": trde_tp,
        "cond_uv": "",
    }
    tr_id = TR.ORDER_BUY if order.side is Side.BUY else TR.ORDER_SELL
    try:
        # idempotent=False — 주문은 5xx/타임아웃 자동 재전송 시 이중 체결 위험.
        data = await client.request(tr_id, PATH_ORDER, body, idempotent=False)
    except Exception as exc:
        if journal is not None:
            # 타임아웃/5xx 는 키움이 접수했을 수 있다 — `failed` 로 닫으면 복구
            # 스캔(find_suspect_orders)에서 빠져 "체결됐는데 봇은 모르는 주문"이
            # 된다. `unknown` 으로 남겨 suspect 로 유지 (2026-06-10 제3자 검토).
            if is_outcome_unknown(exc):
                journal.unknown(client_id=client_id, error=str(exc))
            else:
                journal.failed(client_id=client_id, error=str(exc))
        raise

    order_no = str(data.get("ord_no") or data.get("odno") or "")
    if journal is not None:
        journal.accepted(client_id=client_id, order_no=order_no)

    log.info(
        "order.placed",
        symbol=symbol,
        side=order.side.value,
        qty=order.quantity,
        type=order.order_type.value,
        order_no=order_no,
        client_id=client_id,
    )
    return OrderResult(order_no=order_no, raw=data, client_id=client_id)


async def cancel_order(
    client: KiwoomRestClient,
    *,
    original_order_no: str,
    symbol: str,
    quantity: int,
    exchange: str = "KRX",
) -> OrderResult:
    body = {
        "dmst_stex_tp": exchange,
        "orig_ord_no": original_order_no,
        "stk_cd": symbol,
        "cncl_qty": str(quantity),
    }
    data = await client.request(TR.ORDER_CANCEL, PATH_ORDER, body)
    order_no = str(data.get("ord_no") or data.get("odno") or "")
    log.info("order.canceled", symbol=symbol, original=original_order_no, order_no=order_no)
    return OrderResult(order_no=order_no, raw=data)
