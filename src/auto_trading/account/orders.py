"""주문(매수/매도/정정/취소) 래퍼.

모의투자도 동일 엔드포인트를 사용. 실거래 전환은 `KIWOOM_MODE=real` 로 해야만 가능.
러너가 리스크 매니저 통과한 Order 객체만 여기로 넘긴다.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

import structlog

from ..kiwoom.endpoints import PATH_ORDER, TR
from ..kiwoom.rest import KiwoomRestClient

log = structlog.get_logger(__name__)


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"   # 시장가
    LIMIT = "limit"     # 지정가


@dataclass(frozen=True)
class Order:
    symbol: str
    side: Side
    quantity: int
    order_type: OrderType = OrderType.MARKET
    price: Decimal | None = None   # limit 일 때만
    exchange: str = "KRX"


@dataclass(frozen=True)
class OrderResult:
    order_no: str
    raw: dict


async def place_order(client: KiwoomRestClient, order: Order) -> OrderResult:
    """매수/매도 주문 전송.

    키움 주문 파라미터명:
    - `dmst_stex_tp`: 국내거래소 구분 (KRX/NXT)
    - `stk_cd`: 종목코드
    - `ord_qty`: 주문수량
    - `ord_uv`: 주문단가 (시장가는 "")
    - `trde_tp`: 매매구분 (0 지정가, 3 시장가 등)
    """
    trde_tp = "3" if order.order_type is OrderType.MARKET else "0"
    price_str = "" if order.order_type is OrderType.MARKET else str(order.price or "")

    body = {
        "dmst_stex_tp": order.exchange,
        "stk_cd": order.symbol,
        "ord_qty": str(order.quantity),
        "ord_uv": price_str,
        "trde_tp": trde_tp,
        "cond_uv": "",
    }
    tr_id = TR.ORDER_BUY if order.side is Side.BUY else TR.ORDER_SELL
    data = await client.request(tr_id, PATH_ORDER, body)
    order_no = str(data.get("ord_no") or data.get("odno") or "")

    log.info(
        "order.placed",
        symbol=order.symbol,
        side=order.side.value,
        qty=order.quantity,
        type=order.order_type.value,
        order_no=order_no,
    )
    return OrderResult(order_no=order_no, raw=data)


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
