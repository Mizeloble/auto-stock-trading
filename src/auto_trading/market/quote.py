"""현재가·호가 조회."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .._common import to_decimal
from ..kiwoom.endpoints import PATH_STKINFO, TR
from ..kiwoom.rest import KiwoomRestClient


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: Decimal  # 현재가 (절댓값)
    change: Decimal  # 전일 대비 변동폭 (부호 포함)
    change_pct: Decimal  # 등락률 % (예: 1.23 = +1.23%)
    volume: int  # 누적 거래량


async def get_quote(client: KiwoomRestClient, symbol: str) -> Quote:
    """주식기본정보(`ka10001`) — 현재가/등락률/거래량 포함.

    호가창(`ka10004`) 에는 현재가 필드가 없어서 기본정보 TR을 사용한다.
    키움 응답 필드명이 문서에 따라 약간 다르므로 여러 후보를 순회하며 읽는다.
    """
    data = await client.request(TR.STOCK_BASIC_INFO, PATH_STKINFO, {"stk_cd": symbol})

    def pick(*keys: str) -> object:
        for k in keys:
            if k in data and data[k] not in (None, ""):
                return data[k]
        return None

    return Quote(
        symbol=symbol,
        price=abs(to_decimal(pick("cur_prc", "stck_prpr", "prpr"))),
        change=to_decimal(pick("pred_pre", "prdy_vrss", "chg")),
        change_pct=to_decimal(pick("flu_rt", "prdy_ctrt", "chg_rate")),
        volume=int(to_decimal(pick("trde_qty", "acml_vol", "vol"))),
    )
