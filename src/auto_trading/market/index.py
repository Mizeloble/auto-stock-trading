"""KOSPI/KOSDAQ 종합지수 일봉 + 5/20일선 게이트 (day-trade 매수 게이트용)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from statistics import mean
from typing import Literal

import structlog

from .._common import extract_rows, to_decimal
from ..kiwoom.endpoints import (
    PATH_SECTOR,
    SECTOR_CODE_KOSDAQ,
    SECTOR_CODE_KOSPI,
    TR,
)
from ..kiwoom.rest import KiwoomRestClient

log = structlog.get_logger(__name__)

Market = Literal["KOSPI", "KOSDAQ"]

_SECTOR_CODE: dict[Market, str] = {
    "KOSPI": SECTOR_CODE_KOSPI,
    "KOSDAQ": SECTOR_CODE_KOSDAQ,
}

# SMA 게이트에 필요한 일봉 수. 20일선 + 여유.
SMA_LOOKBACK = 30
SMA_SHORT = 5
SMA_LONG = 20


@dataclass(frozen=True)
class IndexGate:
    """지수 게이트 판정 결과.

    `above_5_and_20=True` 인 시장의 종목만 day-trade 매수 허용.
    데이터 부족이나 응답 실패 시 보수적으로 `False` 를 돌려 매수 차단한다.
    """

    market: Market
    last: float  # 가장 최근 종가
    sma5: float  # 5일 단순이동평균 (계산 불가시 0)
    sma20: float  # 20일 단순이동평균 (계산 불가시 0)
    above_5_and_20: bool  # 마지막 종가가 5/20 SMA 둘 다 위
    bars: int  # 실제로 받은 일봉 수 (디버그용)


def _parse_close(row: dict) -> float | None:
    """업종 일봉 한 행에서 종가를 꺼낸다. 필드명 후보 순회."""
    raw = (
        row.get("cur_prc")
        or row.get("stck_clpr")
        or row.get("close")
        or row.get("clpr")
        or row.get("inds_prc")  # 업종지수 종가 후보
    )
    if raw in (None, ""):
        return None
    return float(abs(to_decimal(raw)))


def _failed_gate(market: Market, reason: str, bars: int = 0) -> IndexGate:
    log.warning("market.index.gate_unavailable", market=market, reason=reason, bars=bars)
    return IndexGate(market=market, last=0.0, sma5=0.0, sma20=0.0, above_5_and_20=False, bars=bars)


async def index_gate(
    client: KiwoomRestClient,
    market: Market,
    *,
    base_dt: date | None = None,
) -> IndexGate:
    """`market` 종합지수 일봉 30봉 → 5/20 SMA → 마지막 종가 위/아래 판정.

    무엇/왜: day-trade 매수 게이트. 약세장에서 거래량 상위만 보고 매수 들어가면 손실 누적.
    어떻게 확인: 단위 테스트에서 5/20 SMA 계산값 + 데이터 부족 시 False 검증.
    """
    body = {
        "inds_cd": _SECTOR_CODE[market],
        "base_dt": (base_dt or date.today()).strftime("%Y%m%d"),
    }
    try:
        page = await client.request(TR.SECTOR_INDEX_DAILY, PATH_SECTOR, body)
    except Exception:
        log.warning("market.index.fetch_failed", market=market, exc_info=True)
        return _failed_gate(market, reason="fetch_failed")

    rows = extract_rows(page)
    closes: list[float] = []
    for row in rows:
        c = _parse_close(row)
        if c is not None:
            closes.append(c)
        if len(closes) >= SMA_LOOKBACK:
            break

    if len(closes) < SMA_LONG:
        return _failed_gate(market, reason="insufficient_bars", bars=len(closes))

    # 키움은 최신 → 과거 순으로 응답하므로 그대로 슬라이싱.
    last = closes[0]
    sma5 = mean(closes[:SMA_SHORT]) if len(closes) >= SMA_SHORT else 0.0
    sma20 = mean(closes[:SMA_LONG])
    above = last > sma5 > 0 and last > sma20 > 0

    gate = IndexGate(
        market=market,
        last=last,
        sma5=sma5,
        sma20=sma20,
        above_5_and_20=above,
        bars=len(closes),
    )
    log.info(
        "market.index.gate",
        market=market,
        last=last,
        sma5=round(sma5, 2),
        sma20=round(sma20, 2),
        above=above,
        bars=len(closes),
    )
    return gate


__all__ = ["SMA_LONG", "SMA_LOOKBACK", "SMA_SHORT", "IndexGate", "Market", "index_gate"]
