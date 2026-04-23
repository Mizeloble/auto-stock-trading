"""현재가·호가 조회."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..kiwoom.endpoints import PATH_MARKET, TR
from ..kiwoom.rest import KiwoomRestClient


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: Decimal        # 현재가 (절댓값)
    change: Decimal       # 전일 대비 변동폭 (부호 포함)
    change_pct: Decimal   # 등락률 % (예: 1.23 = +1.23%)
    volume: int           # 누적 거래량


def _to_decimal(raw: object) -> Decimal:
    """키움 응답 문자열(부호/콤마 포함) → Decimal."""
    if raw is None:
        return Decimal(0)
    s = str(raw).replace(",", "").replace("+", "").strip()
    if s in ("", "-"):
        return Decimal(0)
    try:
        return Decimal(s)
    except Exception:
        return Decimal(0)


async def get_quote(client: KiwoomRestClient, symbol: str) -> Quote:
    """주식호가요청(`ka10004`) — 현재가/등락률/거래량 포함.

    키움 응답 필드명이 문서에 따라 약간 다르므로 여러 후보를 순회하며 읽는다.
    """
    data = await client.request(TR.QUOTE_CURRENT, PATH_MARKET, {"stk_cd": symbol})

    def pick(*keys: str) -> object:
        for k in keys:
            if k in data and data[k] not in (None, ""):
                return data[k]
        return None

    return Quote(
        symbol=symbol,
        price=abs(_to_decimal(pick("cur_prc", "stck_prpr", "prpr"))),
        change=_to_decimal(pick("pred_pre", "prdy_vrss", "chg")),
        change_pct=_to_decimal(pick("flu_rt", "prdy_ctrt", "chg_rate")),
        volume=int(_to_decimal(pick("trde_qty", "acml_vol", "vol"))),
    )
