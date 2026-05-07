"""KOSPI/KOSDAQ 종합지수 — 일봉 5/20 게이트 + 일중 모멘텀 트래커."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from statistics import mean
from typing import Literal

import structlog

from .._common import KST, extract_rows, to_decimal
from ..kiwoom.endpoints import (
    INDEX_MRKT_TP_KOSDAQ,
    INDEX_MRKT_TP_KOSPI,
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

# ka20009 의 mrkt_tp 코드 — 순위 TR 의 mrkt_tp(001/101) 와 다른 체계.
_INDEX_MRKT_TP: dict[Market, str] = {
    "KOSPI": INDEX_MRKT_TP_KOSPI,
    "KOSDAQ": INDEX_MRKT_TP_KOSDAQ,
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
    """업종 일봉 한 행에서 종가를 꺼낸다. 필드명 후보 순회.

    probe 2026-05-03 실측: ka20009 의 list `inds_cur_prc_daly_rept` 행 키는
    `cur_prc_n` (suffix `_n`). 다른 TR 가 share 할 수 있도록 폭넓은 후보 유지.
    """
    raw = (
        row.get("cur_prc_n")  # ka20009 업종 일봉 (probe 2026-05-03 확정)
        or row.get("cur_prc")
        or row.get("stck_clpr")
        or row.get("close")
        or row.get("clpr")
        or row.get("inds_prc")
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
    # ka20009 필수 파라미터 (probe 2026-05-03 확정):
    # - mrkt_tp: 시장 구분 (0=KOSPI, 1=KOSDAQ) — 누락 시 1511 에러
    # - inds_cd: 업종 코드 (001=KOSPI 종합, 101=KOSDAQ 종합)
    # - base_dt: YYYYMMDD
    body = {
        "mrkt_tp": _INDEX_MRKT_TP[market],
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


# ---- 23단계 Phase E · 일중 모멘텀 트래커 -----------------------------------


@dataclass(frozen=True)
class IndexMomentum:
    """일중 지수 모멘텀 결과. lookback_return_pct=None 이면 history 부족."""

    market: Market
    last: float
    lookback_min: int
    lookback_return_pct: float | None
    ts: datetime


@dataclass
class IndexMomentumTracker:
    """KOSPI/KOSDAQ 지수 가격 시계열을 인메모리로 들고 일중 모멘텀 계산.

    무엇/왜:
    - paper-day 한 프로세스 안에서 인스턴스 1개를 공유 (orchestrator 가 소유 예정).
    - `push(market, ts, price)` 는 외부 호출 없음 — fetch 와 분리해 단위 테스트 용이.
    - `momentum(market, lookback_min)` 은 인메모리 history 만 보고 직전 N분 수익률 계산.
    - history 는 `history_minutes` 분 이상 오래되면 자동 정리 (메모리 누수 방지).

    어떻게 확인: tests/test_market_index_momentum.py 가 push/momentum 매칭 검증.
    """

    history_minutes: int = 30
    match_tolerance_sec: int = 90
    _history: dict[str, list[tuple[datetime, float]]] = field(default_factory=dict)

    def push(self, market: Market, ts: datetime, price: float) -> None:
        """가격 1포인트 추가. cutoff 이후 데이터만 유지."""
        if price <= 0:
            return
        series = self._history.setdefault(market, [])
        series.append((ts, price))
        cutoff = ts - timedelta(minutes=self.history_minutes)
        # 일반적으로 시간순 push 라 앞에서 자르지만, 안전하게 sort + filter.
        series[:] = sorted([(t, p) for t, p in series if t >= cutoff])

    def momentum(
        self,
        market: Market,
        *,
        lookback_min: int = 5,
        now: datetime | None = None,
    ) -> IndexMomentum | None:
        """`lookback_min` 분 전 가격과 비교해 수익률 계산. history 없으면 None."""
        series = self._history.get(market, [])
        if not series:
            return None
        latest_ts, latest_price = series[-1]
        ts = now or latest_ts
        target = ts - timedelta(minutes=lookback_min)
        past_price = self._closest_price(series, target)
        if past_price is None or past_price <= 0:
            return IndexMomentum(
                market=market,
                last=latest_price,
                lookback_min=lookback_min,
                lookback_return_pct=None,
                ts=ts,
            )
        pct = (latest_price / past_price - 1.0) * 100.0
        return IndexMomentum(
            market=market,
            last=latest_price,
            lookback_min=lookback_min,
            lookback_return_pct=pct,
            ts=ts,
        )

    def _closest_price(
        self, series: list[tuple[datetime, float]], target: datetime
    ) -> float | None:
        """target 시각에 가장 가까운 가격 — `match_tolerance_sec` 안에 있을 때만 반환."""
        best_price: float | None = None
        best_diff = self.match_tolerance_sec + 1
        for ts, price in series:
            diff = abs((ts - target).total_seconds())
            if diff <= self.match_tolerance_sec and diff < best_diff:
                best_diff = int(diff)
                best_price = price
        return best_price


async def fetch_latest_index_price(client: KiwoomRestClient, market: Market) -> float | None:
    """ka20009 응답의 가장 최근 행 close 한 개만 꺼냄 (오늘 일봉 = 진행 중인 현재 지수값).

    무엇/왜: index_gate 와 같은 TR 을 호출하지만, momentum 에는 history 한 점씩만 필요.
    실패 시 None — 호출자가 push 안 하도록.
    """
    body = {
        "mrkt_tp": _INDEX_MRKT_TP[market],
        "inds_cd": _SECTOR_CODE[market],
        "base_dt": date.today().strftime("%Y%m%d"),
    }
    try:
        page = await client.request(TR.SECTOR_INDEX_DAILY, PATH_SECTOR, body)
    except Exception:
        log.warning("market.index.momentum_fetch_failed", market=market, exc_info=True)
        return None
    rows = extract_rows(page)
    for row in rows:
        c = _parse_close(row)
        if c is not None:
            return c
    return None


async def update_index_momentum(
    client: KiwoomRestClient,
    tracker: IndexMomentumTracker,
    market: Market,
    *,
    lookback_min: int = 5,
    now: datetime | None = None,
) -> IndexMomentum | None:
    """fetch + push + momentum 한 번에. 호출자가 1분 주기로 사용 (Phase F orchestrator)."""
    price = await fetch_latest_index_price(client, market)
    if price is None:
        return None
    ts = now or datetime.now(KST)
    tracker.push(market, ts, price)
    result = tracker.momentum(market, lookback_min=lookback_min, now=ts)
    if result is not None:
        log.info(
            "market.index.momentum",
            market=market,
            last=price,
            lookback_min=lookback_min,
            return_pct=(
                round(result.lookback_return_pct, 4)
                if result.lookback_return_pct is not None
                else None
            ),
        )
    return result


__all__ = [
    "SMA_LONG",
    "SMA_LOOKBACK",
    "SMA_SHORT",
    "IndexGate",
    "IndexMomentum",
    "IndexMomentumTracker",
    "Market",
    "fetch_latest_index_price",
    "index_gate",
    "update_index_momentum",
]
