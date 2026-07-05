"""거래량 상위 종목 조회 (day-trade 발굴용)."""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Literal

import structlog

from .._common import extract_rows, to_decimal
from .._common import normalize_symbol as _normalize_symbol
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


# 종목 코드 정규화는 _common.normalize_symbol 로 통합 (probe 2026-05-03 + 2026-05-08
# 청산 거절 회귀 — 잔고/feed/주문 모든 경계에서 동일 정규화 적용).


# day-trade 부적합 종목 휴리스틱 — probe 2026-05-03 결과 KOSPI 거래량 상위가
# KODEX 인버스 / WTI 원유 ETN / 신한 SPAC 등 파생·특수목적 종목 위주라 일반 주식
# day-trade 의도와 안 맞음. 이름 기반 보수적 필터.
_ETF_BRANDS_UPPER = (
    "KODEX",
    "TIGER",
    "KBSTAR",
    "KINDEX",
    "SOL",
    "HANARO",
    "ARIRANG",
    "KOSEF",
    "PLUS",
    "RISE",
    "ACE",
    "WOORI",
    "TIMEFOLIO",
    "히어로즈",
    "마이티",
)
# 한국어 키워드 — case-insensitive 비교 위해 .upper() 후 매칭하므로 한글은 그대로 유지.
_ETF_KEYWORDS = ("ETN", "선물인버스", "선물레버리지", "스팩")


def _is_etf_or_etn(name: str) -> bool:
    """이름 기반 ETF/ETN/SPAC 휴리스틱. 완벽하지 않지만 거래량 상위에 자주 끼는 케이스를 잡는다.

    무엇/왜: probe 실측에서 일반 주식 사이에 인버스 ETF·ETN·SPAC 이 거래량 상위를
    차지해 day-trade 발굴기가 이런 종목을 매수하게 되는 문제 회피.
    어떻게 확인: 단위 테스트가 KODEX 인버스 / WTI ETN / 신한제18호스팩 / 삼성전자
    등 실측 케이스로 분기 검증.
    """
    if not name:
        return False
    upper = name.upper()
    # 영문 ETF 브랜드는 단어 시작 매칭(공백 또는 끝), 한글 브랜드는 부분 매칭.
    for brand in _ETF_BRANDS_UPPER:
        if brand.isascii():
            if upper.startswith(brand + " ") or upper == brand:
                return True
        elif brand in name:
            return True
    # ETN / 선물인버스 / 선물레버리지 / 스팩 키워드는 위치 무관 부분 매칭.
    return any(kw in upper or kw in name for kw in _ETF_KEYWORDS)


def _parse_row(row: dict, market: Market) -> RankedSymbol | None:
    """키움 응답 한 행 → RankedSymbol. 필드명이 문서 버전마다 달라 후보를 순회한다."""
    raw_symbol = _pick(row, "stk_cd", "stkcd", "symbol")
    raw_name = _pick(row, "stk_nm", "hts_kor_isnm", "name")
    raw_volume = _pick(row, "trde_qty", "acml_vol", "volume")
    raw_price = _pick(row, "cur_prc", "stck_prpr", "prpr", "price")
    if not raw_symbol or raw_volume is None:
        return None
    return RankedSymbol(
        symbol=_normalize_symbol(raw_symbol),
        market=market,
        name=str(raw_name or "").strip(),
        volume=int(to_decimal(raw_volume)),
        price=float(abs(to_decimal(raw_price))),
    )


async def _fetch_market(
    client: KiwoomRestClient,
    market: Market,
    *,
    sort_tp: str = "1",
) -> list[RankedSymbol]:
    """단일 시장(KOSPI 또는 KOSDAQ) 순위 상위 행을 가져온다.

    `sort_tp`: 1=거래량(주식 수), 2=거래대금. 응답 길이는 키움이 정함(보통 100).
    잘림(top_n) 과 ETF 필터는 호출자가 처리.
    """
    # ka10030 (당일거래량상위) 필수 파라미터 — probe(2026-05-03) 로 확정.
    # crd_tp / trde_qty_tp / pric_tp / trde_prica_tp / mrkt_open_tp 가 빠지면
    # "1511: 필수 입력 값에 값이 존재하지 않습니다" 로 거절. 모두 "전체"(0/000) 기본.
    body = {
        "mrkt_tp": _MRKT_TP[market],
        "sort_tp": sort_tp,  # 1=거래량, 2=거래대금
        "mang_stk_incls": "0",  # 관리종목 제외
        "crd_tp": "0",  # 신용 구분: 0=전체
        "trde_qty_tp": "0",  # 거래량 구분: 0=전체
        "pric_tp": "0",  # 가격 구분: 0=전체
        "trde_prica_tp": "0",  # 거래대금 구분: 0=전체
        "mrkt_open_tp": "0",  # 장운영 구분: 0=전체
        "stex_tp": "3",  # 거래소: 1=KRX 2=NXT 3=통합
    }
    page = await client.request(TR.RANK_VOLUME, PATH_RANK, body)
    rows = extract_rows(page)
    parsed: list[RankedSymbol] = []
    for row in rows:
        item = _parse_row(row, market)
        if item is not None:
            parsed.append(item)
        # ETF 필터는 호출자(top_volume/top_value) 단계에서 적용 — 여기서는 매핑만.
    return parsed


async def _merged_top(
    client: KiwoomRestClient,
    *,
    sort_tp: str,
    sort_key: Callable[[RankedSymbol], float],
    log_event: str,
    top_n: int,
    markets: tuple[Market, ...],
    exclude_etf: bool,
    exclude_symbols: Collection[str],
) -> list[RankedSymbol]:
    """두 시장 순위를 합쳐 필터(ETF/회피) 후 `sort_key` desc 상위 `top_n`.

    top_volume/top_value 의 공통 몸통 — 정렬 축(sort_tp·sort_key)만 갈라진다.
    """
    avoid = {s for s in exclude_symbols if s}
    merged: list[RankedSymbol] = []
    for market in markets:
        try:
            rows = await _fetch_market(client, market, sort_tp=sort_tp)
        except Exception:
            log.warning("market.rank.fetch_failed", market=market, exc_info=True)
            continue
        merged.extend(rows)

    excluded: list[tuple[str, str]] = []
    skipped_known = 0
    if exclude_etf or avoid:
        kept: list[RankedSymbol] = []
        for r in merged:
            if r.symbol in avoid:
                skipped_known += 1
            elif exclude_etf and _is_etf_or_etn(r.name):
                excluded.append((r.symbol, r.name))
            else:
                kept.append(r)
        merged = kept

    merged.sort(key=sort_key, reverse=True)
    chosen = merged[:top_n]
    log.info(
        log_event,
        markets=list(markets),
        top_n=top_n,
        exclude_etf=exclude_etf,
        chosen=[(r.symbol, r.market, r.volume) for r in chosen],
        excluded_count=len(excluded),
        skipped_known=skipped_known,
    )
    return chosen


async def top_volume(
    client: KiwoomRestClient,
    *,
    top_n: int = 3,
    markets: tuple[Market, ...] = ("KOSPI", "KOSDAQ"),
    exclude_etf: bool = True,
    exclude_symbols: Collection[str] = (),
) -> list[RankedSymbol]:
    """KOSPI/KOSDAQ 거래량 상위를 합쳐 거래량 desc 정렬 후 상위 `top_n` 반환.

    `exclude_etf=True` (기본) 면 ETF/ETN/SPAC 류 종목을 사전에 제외 — day-trade 의 일반
    주식 매매 의도에 맞게. 반대로 인버스 ETF 도 매매 대상으로 두려면 False.

    `exclude_symbols` 에 든 종목은 순위에서 통째로 빼고 다음 순위가 그 자리를
    메운다(거래불가 이력·회피 목록 등). 단순 절사가 아니라 *제외 후 top_n* 이라
    슬롯을 낭비하지 않는다.

    필터 후 부족할 일은 거의 없음 — 키움이 시장당 100개를 돌려주므로.
    """
    return await _merged_top(
        client,
        sort_tp="1",
        sort_key=lambda r: r.volume,
        log_event="market.rank.top_volume",
        top_n=top_n,
        markets=markets,
        exclude_etf=exclude_etf,
        exclude_symbols=exclude_symbols,
    )


async def top_value(
    client: KiwoomRestClient,
    *,
    top_n: int = 3,
    markets: tuple[Market, ...] = ("KOSPI", "KOSDAQ"),
    exclude_etf: bool = True,
    exclude_symbols: Collection[str] = (),
) -> list[RankedSymbol]:
    """거래대금 상위 (`sort_tp=2`) — 발굴 대안 신호 연구용 read-only 조회.

    무엇/왜: 거래량(주식 수) 순위는 저가주가 구조적으로 상위를 차지한다
    (2026-07-04 선정 알파 검증). 거래대금 순위가 더 나은 발굴 원천인지는
    과거로 거슬러 조회할 수 없어(당일 순위만 제공) 매일 기록해 둬야 나중에
    반사실 비교가 가능하다 — 기록은 daytrade.cli_helpers 가 담당.

    두 시장 병합 정렬 키는 volume x price (응답의 거래대금 필드가 TR 버전마다
    달라 근사 사용 — 시장 내 순서는 키움 정렬이 이미 거래대금 기준).
    """
    return await _merged_top(
        client,
        sort_tp="2",
        sort_key=lambda r: r.volume * r.price,
        log_event="market.rank.top_value",
        top_n=top_n,
        markets=markets,
        exclude_etf=exclude_etf,
        exclude_symbols=exclude_symbols,
    )


__all__ = ["Market", "RankedSymbol", "top_value", "top_volume"]
