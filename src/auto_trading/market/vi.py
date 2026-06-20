"""VI(변동성완화장치) 발동종목 조회 — read-only 위험신호 로깅용.

키움 `ka10054`(변동성완화장치발동종목요청). day-trade 매매 판단엔 쓰지 않고,
시장 위험신호(어떤 종목이 급변동으로 VI 가 걸렸는지)를 사후 로깅·관찰하는 용도다.

mock 실측(probe 2026-06-21)으로 필수 파라미터 12개와 응답 스키마(`motn_stk`
리스트, 행 키 stk_cd/stk_nm/viaplc_tp/motn_pric/...)를 확정했다. 필드명이 빠지면
키움이 `1511 필수 입력 값` 으로 거절하므로 `_body()` 의 키 집합을 줄이지 말 것.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

import structlog

from .._common import extract_rows, normalize_symbol, to_decimal
from ..kiwoom.endpoints import (
    MARKET_TP_ALL,
    MARKET_TP_KOSDAQ,
    MARKET_TP_KOSPI,
    PATH_STKINFO,
    TR,
)
from ..kiwoom.rest import KiwoomRestClient

log = structlog.get_logger(__name__)

Market = Literal["ALL", "KOSPI", "KOSDAQ"]

_MRKT_TP: dict[Market, str] = {
    "ALL": MARKET_TP_ALL,
    "KOSPI": MARKET_TP_KOSPI,
    "KOSDAQ": MARKET_TP_KOSDAQ,
}


@dataclass(frozen=True)
class ViStock:
    """VI 가 발동된 종목 한 건. 위험신호 로그의 한 행."""

    symbol: str
    name: str
    kind: str  # 발동유형: "동적" 또는 "정적" (viaplc_tp)
    trigger_price: Decimal  # 발동가격 (motn_pric, 절댓값)
    disparity_pct: Decimal  # 발동 기준 괴리율 % (동적이면 동적, 정적이면 정적)
    release_time: str  # VI 해제 예정시각 HHMMSS (virelis_time)
    trigger_count: int  # 당일 누적 발동 횟수 (vimotn_cnt)
    volume: int  # 누적 거래량 (acc_trde_qty)
    exchange: str  # 거래소 구분 (stex_tp, 예: "KRX")


def _body(mrkt_tp: str) -> dict[str, str]:
    """ka10054 필수 파라미터 12개. 전부 "전체"(0) 기본 — probe 2026-06-21 확정.

    하나라도 빠지면 키움이 `1511 필수 입력 값에 값이 존재하지 않습니다` 로 거절.
    """
    return {
        "mrkt_tp": mrkt_tp,  # 시장구분 (000=전체 / 001=KOSPI / 101=KOSDAQ)
        "bf_mkrt_tp": "0",  # 장전구분
        "motn_tp": "0",  # 발동구분: 0=전체
        "skip_stk": "0",  # 제외종목
        "trde_qty_tp": "0",  # 거래량 구분: 0=전체
        "min_trde_qty": "0",
        "max_trde_qty": "0",
        "trde_prica_tp": "0",  # 거래대금 구분: 0=전체
        "min_trde_prica": "0",
        "max_trde_prica": "0",
        "motn_drc": "0",  # 발동방향: 0=전체
        "stex_tp": "0",  # 거래소 구분
    }


def _parse_row(row: dict) -> ViStock | None:
    """`motn_stk` 한 행 → ViStock. 발동유형에 맞는 괴리율을 고른다."""
    raw_symbol = row.get("stk_cd")
    if not raw_symbol:
        return None
    kind = str(row.get("viaplc_tp") or "").strip()
    # 동적 VI 는 동적 괴리율(dynm_dispty_rt), 정적 VI 는 정적 괴리율(static_dispty_rt).
    dispty = row.get("dynm_dispty_rt") if kind == "동적" else row.get("static_dispty_rt")
    return ViStock(
        symbol=normalize_symbol(raw_symbol),
        name=str(row.get("stk_nm") or "").strip(),
        kind=kind,
        trigger_price=abs(to_decimal(row.get("motn_pric"))),
        disparity_pct=to_decimal(dispty),
        release_time=str(row.get("virelis_time") or "").strip(),
        trigger_count=int(to_decimal(row.get("vimotn_cnt"))),
        volume=int(to_decimal(row.get("acc_trde_qty"))),
        exchange=str(row.get("stex_tp") or "").strip(),
    )


async def vi_triggered(
    client: KiwoomRestClient,
    *,
    market: Market = "ALL",
) -> list[ViStock]:
    """현재 VI 가 발동돼 있는 종목 리스트를 가져온다 (read-only).

    응답 길이는 키움이 정한다(실측 시 시장 전체 100건). 매매에 쓰지 말고
    로깅·관찰용으로만 사용 — 이 모듈은 `market/` 의 읽기 전용 규칙을 따른다.
    """
    page = await client.request(TR.VI_TRIGGERED, PATH_STKINFO, _body(_MRKT_TP[market]))
    rows = extract_rows(page)
    parsed: list[ViStock] = []
    for row in rows:
        item = _parse_row(row)
        if item is not None:
            parsed.append(item)
    log.info("market.vi.triggered", market=market, count=len(parsed))
    return parsed


__all__ = ["Market", "ViStock", "vi_triggered"]
