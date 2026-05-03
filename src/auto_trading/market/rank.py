"""거래량 상위 종목 조회 (day-trade 발굴용)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import structlog

from .._common import extract_rows, to_decimal
from ..kiwoom.endpoints import (
    MARKET_TP_KOSDAQ,
    MARKET_TP_KOSPI,
    PATH_RANK,
    TR,
)
from ..kiwoom.rest import KiwoomRestClient

log = structlog.get_logger(__name__)

Market = Literal["KOSPI", "KOSDAQ"]

_MRKT_TP: dict[Market, str] = {
    "KOSPI": MARKET_TP_KOSPI,
    "KOSDAQ": MARKET_TP_KOSDAQ,
}


@dataclass(frozen=True)
class RankedSymbol:
    """거래량 순위 한 행. day-trade 발굴기 입력으로 쓴다."""

    symbol: str
    market: Market
    name: str
    volume: int  # 누적 거래량
    price: float  # 현재가 (절댓값)


def _pick(row: dict, *keys: str) -> object:
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    return None


def _parse_row(row: dict, market: Market) -> RankedSymbol | None:
    """키움 응답 한 행 → RankedSymbol. 필드명이 문서 버전마다 달라 후보를 순회한다."""
    raw_symbol = _pick(row, "stk_cd", "stkcd", "symbol")
    raw_name = _pick(row, "stk_nm", "hts_kor_isnm", "name")
    raw_volume = _pick(row, "trde_qty", "acml_vol", "volume")
    raw_price = _pick(row, "cur_prc", "stck_prpr", "prpr", "price")
    if not raw_symbol or raw_volume is None:
        return None
    return RankedSymbol(
        symbol=str(raw_symbol).strip().lstrip("A"),  # 키움이 'A005930' 처럼 prefix 붙이기도 함
        market=market,
        name=str(raw_name or "").strip(),
        volume=int(to_decimal(raw_volume)),
        price=float(abs(to_decimal(raw_price))),
    )


async def _fetch_market(
    client: KiwoomRestClient,
    market: Market,
    *,
    top_n: int,
) -> list[RankedSymbol]:
    """단일 시장(KOSPI 또는 KOSDAQ) 거래량 상위 행을 가져온다.

    `mrkt_tp` 외 파라미터(정렬 기준·관리종목 포함 등)는 키움 문서 기본값에 의존.
    probe 후 필요한 파라미터를 추가한다.
    """
    body = {
        "mrkt_tp": _MRKT_TP[market],
        "sort_tp": "1",  # 거래량 기준 (probe 로 확정)
        "mang_stk_incls": "0",  # 관리종목 제외
        "stex_tp": "3",  # 통합 (KRX+NXT)
    }
    page = await client.request(TR.RANK_VOLUME, PATH_RANK, body)
    rows = extract_rows(page)
    parsed: list[RankedSymbol] = []
    for row in rows:
        item = _parse_row(row, market)
        if item is not None:
            parsed.append(item)
        if len(parsed) >= top_n:
            break
    return parsed


async def top_volume(
    client: KiwoomRestClient,
    *,
    top_n: int = 3,
    markets: tuple[Market, ...] = ("KOSPI", "KOSDAQ"),
) -> list[RankedSymbol]:
    """KOSPI/KOSDAQ 거래량 상위를 합쳐 거래량 desc 정렬 후 상위 `top_n` 반환.

    각 시장은 `top_n * 2` 만큼 후보를 받아온다 — 두 시장 합쳤을 때 한쪽이 잘려도
    상위 N 은 채워지도록.
    """
    per_market_pull = max(top_n * 2, top_n)
    merged: list[RankedSymbol] = []
    for market in markets:
        try:
            rows = await _fetch_market(client, market, top_n=per_market_pull)
        except Exception:
            log.warning("market.rank.fetch_failed", market=market, exc_info=True)
            continue
        merged.extend(rows)
    merged.sort(key=lambda r: r.volume, reverse=True)
    chosen = merged[:top_n]
    log.info(
        "market.rank.top_volume",
        markets=list(markets),
        top_n=top_n,
        chosen=[(r.symbol, r.market, r.volume) for r in chosen],
    )
    return chosen


__all__ = ["Market", "RankedSymbol", "top_volume"]
