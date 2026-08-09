"""거래량 상위 종목 조회 (day-trade 발굴용)."""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Literal

import structlog

from .._common import extract_rows, to_decimal
from .._common import normalize_symbol as _normalize_symbol
from ..kiwoom.endpoints import (
    INVESTOR_TP_FOREIGN,
    INVESTOR_TP_INSTITUTION,
    MARKET_TP_KOSDAQ,
    MARKET_TP_KOSPI,
    PATH_RANK,
    TR,
)
from ..kiwoom.rest import KiwoomRestClient

log = structlog.get_logger(__name__)

Market = Literal["KOSPI", "KOSDAQ"]
NetBuyInvestor = Literal["foreign", "institution"]

_MRKT_TP: dict[Market, str] = {
    "KOSPI": MARKET_TP_KOSPI,
    "KOSDAQ": MARKET_TP_KOSDAQ,
}

_INVESTOR_TP: dict[NetBuyInvestor, str] = {
    "foreign": INVESTOR_TP_FOREIGN,
    "institution": INVESTOR_TP_INSTITUTION,
}


@dataclass(frozen=True)
class RankedSymbol:
    """순위 TR 응답 한 행. day-trade 발굴기 입력으로 쓴다.

    `volume`/`price` 는 거래량·거래대금 순위(ka10030)가 채우고, 순매수 순위(ka10065)
    응답엔 두 필드가 아예 없어 0 이 된다 — 그 축의 실제 지표는 `metric` 이 담는다.
    `rank` 는 시장 내 1-based 응답 순서(키움이 이미 그 축으로 정렬해 준 순위).
    """

    symbol: str
    market: Market
    name: str
    volume: int  # 누적 거래량 (미제공 TR 은 0)
    price: float  # 현재가 (절댓값, 미제공 TR 은 0.0)
    metric: float | None = None  # TR 고유 순위 지표 (순매수 수량 등). 부호 유지.
    rank: int | None = None  # 시장 내 1-based 순위 (응답 순서)


@dataclass(frozen=True)
class RankSource:
    """순위 TR 한 축의 요청·해석 사양.

    무엇/왜: 순위 TR 마다 api-id 와 필수 바디가 완전히 달라(ka10030 은 8개,
    ka10065 는 3개) 조회 함수에 하드코딩하면 축을 늘릴 때마다 분기가 생긴다.
    축의 차이를 이 값 객체로 모아 `_fetch_market`/`_merged_top` 몸통은 하나로 유지.

    - `body`: `mrkt_tp` 를 뺀 고정 바디 (시장은 호출 시점에 주입).
    - `sort_key`: 두 시장 병합 후 desc 정렬 키.
    - `metric_keys`: 행에서 `metric` 을 뽑을 필드명 후보 (문서 버전차 흡수).
    """

    tr_id: str
    body: dict[str, str]
    sort_key: Callable[[RankedSymbol], float]
    log_event: str
    metric_keys: tuple[str, ...] = ()


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


def _parse_row(
    row: dict,
    market: Market,
    *,
    metric_keys: tuple[str, ...] = (),
    rank: int | None = None,
) -> RankedSymbol | None:
    """키움 응답 한 행 → RankedSymbol. 필드명이 문서 버전마다 달라 후보를 순회한다."""
    raw_symbol = _pick(row, "stk_cd", "stkcd", "symbol")
    raw_name = _pick(row, "stk_nm", "hts_kor_isnm", "name")
    raw_volume = _pick(row, "trde_qty", "acml_vol", "volume")
    raw_price = _pick(row, "cur_prc", "stck_prpr", "prpr", "price")
    raw_metric = _pick(row, *metric_keys) if metric_keys else None
    # 순위 지표가 하나도 없는 행은 어느 축으로도 정렬할 수 없어 버린다.
    # (거래량 축은 trde_qty, 순매수 축은 metric_keys 가 각각 필수 역할)
    if not raw_symbol or (raw_volume is None and raw_metric is None):
        return None
    return RankedSymbol(
        symbol=_normalize_symbol(raw_symbol),
        market=market,
        name=str(raw_name or "").strip(),
        volume=int(to_decimal(raw_volume)) if raw_volume is not None else 0,
        price=float(abs(to_decimal(raw_price))),
        # 순매수는 음수도 의미가 있으므로 abs 를 씌우지 않는다.
        metric=float(to_decimal(raw_metric)) if raw_metric is not None else None,
        rank=rank,
    )


async def _fetch_market(
    client: KiwoomRestClient,
    market: Market,
    *,
    source: RankSource,
) -> list[RankedSymbol]:
    """단일 시장(KOSPI 또는 KOSDAQ) 순위 상위 행을 가져온다.

    응답 길이는 키움이 정함(보통 100). 잘림(top_n) 과 ETF 필터는 호출자가 처리.
    """
    body = {**source.body, "mrkt_tp": _MRKT_TP[market]}
    page = await client.request(source.tr_id, PATH_RANK, body)
    rows = extract_rows(page)
    parsed: list[RankedSymbol] = []
    for idx, row in enumerate(rows, start=1):
        item = _parse_row(row, market, metric_keys=source.metric_keys, rank=idx)
        if item is not None:
            parsed.append(item)
        # ETF 필터는 호출자(top_volume 등) 단계에서 적용 — 여기서는 매핑만.
    return parsed


# ka10030 (당일거래량상위) 필수 파라미터 — probe(2026-05-03) 로 확정.
# crd_tp / trde_qty_tp / pric_tp / trde_prica_tp / mrkt_open_tp 가 빠지면
# "1511: 필수 입력 값에 값이 존재하지 않습니다" 로 거절. 모두 "전체"(0/000) 기본.
_RANK_VOLUME_BODY = {
    "mang_stk_incls": "0",  # 관리종목 제외
    "crd_tp": "0",  # 신용 구분: 0=전체
    "trde_qty_tp": "0",  # 거래량 구분: 0=전체
    "pric_tp": "0",  # 가격 구분: 0=전체
    "trde_prica_tp": "0",  # 거래대금 구분: 0=전체
    "mrkt_open_tp": "0",  # 장운영 구분: 0=전체
    "stex_tp": "3",  # 거래소: 1=KRX 2=NXT 3=통합
}

_SOURCE_VOLUME = RankSource(
    tr_id=TR.RANK_VOLUME,
    body={**_RANK_VOLUME_BODY, "sort_tp": "1"},  # 1=거래량
    sort_key=lambda r: r.volume,
    log_event="market.rank.top_volume",
)

_SOURCE_VALUE = RankSource(
    tr_id=TR.RANK_VOLUME,
    body={**_RANK_VOLUME_BODY, "sort_tp": "2"},  # 2=거래대금
    sort_key=lambda r: r.volume * r.price,
    log_event="market.rank.top_value",
)


def _net_buy_source(investor: NetBuyInvestor) -> RankSource:
    """ka10065 장중 투자자별 순매수 상위 — 투자자 구분만 다른 축.

    병합 정렬 키가 `metric`(순매수 수량)이 아니라 **시장 내 순위**인 이유:
    KOSPI 와 KOSDAQ 의 순매수 '수량' 은 주가 수준이 달라 직접 비교하면 저가주가
    구조적으로 상위를 독식한다(거래량 순위가 겪는 바로 그 편향). 키움이 각 시장
    안에서 이미 이 축으로 정렬해 주므로, 순위를 그대로 존중해 두 시장을 번갈아
    끼워 넣는다(KOSPI 1위 · KOSDAQ 1위 · KOSPI 2위 …). 원본 `metric`/`rank`/`market`
    은 레코드에 그대로 남기므로 분석 단계에서 다른 병합 규칙으로 재구성할 수 있다.
    """
    return RankSource(
        tr_id=TR.RANK_INTRADAY_INVESTOR,
        body={
            "trde_tp": "1",  # 1=순매수
            "orgn_tp": _INVESTOR_TP[investor],
        },
        sort_key=lambda r: -(r.rank if r.rank is not None else 10_000),
        log_event=f"market.rank.top_net_buy.{investor}",
        metric_keys=("netslmt", "netprps_qty", "netprps"),
    )


async def _merged_top(
    client: KiwoomRestClient,
    *,
    source: RankSource,
    top_n: int,
    markets: tuple[Market, ...],
    exclude_etf: bool,
    exclude_symbols: Collection[str],
) -> list[RankedSymbol]:
    """두 시장 순위를 합쳐 필터(ETF/회피) 후 `source.sort_key` desc 상위 `top_n`.

    모든 순위 축의 공통 몸통 — TR·바디·정렬 축의 차이는 `source` 가 전부 흡수한다.
    """
    avoid = {s for s in exclude_symbols if s}
    merged: list[RankedSymbol] = []
    last_error: Exception | None = None
    failed = 0
    for market in markets:
        try:
            rows = await _fetch_market(client, market, source=source)
        except Exception as exc:
            log.warning("market.rank.fetch_failed", market=market, exc_info=True)
            last_error = exc
            failed += 1
            continue
        merged.extend(rows)

    # 전 시장 조회가 실패했으면 "오늘은 후보가 없다" 가 아니라 시세 조회 자체가 불통이다.
    # 빈 리스트로 돌려주면 호출자가 정상적인 무거래일로 오인해 조용히 종료한다 —
    # 2026-07-27~29 토큰 만료 때 3거래일간 무거래·무알림으로 지나간 원인. 원인 예외를
    # 그대로 올려 상위(delayed_discover)가 실패로 다루고 사용자에게 푸시하게 한다.
    # 일부 시장만 실패한 경우는 종전대로 남은 시장으로 진행 (부분 가용성 유지).
    if last_error is not None and failed == len(markets):
        raise last_error

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

    merged.sort(key=source.sort_key, reverse=True)
    chosen = merged[:top_n]
    log.info(
        source.log_event,
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
        source=_SOURCE_VOLUME,
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
        source=_SOURCE_VALUE,
        top_n=top_n,
        markets=markets,
        exclude_etf=exclude_etf,
        exclude_symbols=exclude_symbols,
    )


async def top_net_buy(
    client: KiwoomRestClient,
    *,
    investor: NetBuyInvestor = "foreign",
    top_n: int = 3,
    markets: tuple[Market, ...] = ("KOSPI", "KOSDAQ"),
    exclude_etf: bool = True,
    exclude_symbols: Collection[str] = (),
) -> list[RankedSymbol]:
    """장중 투자자별 순매수 상위 (`ka10065`) — 발굴 대안 신호 연구용 read-only 조회.

    무엇/왜: 거래량·거래대금 순위는 둘 다 "얼마나 많이 거래됐나" 라는 한 축의
    변형이라 풀이 크게 겹쳤고, 그래서 2026-07-31 거래대금 A/B 는 검정력이 안 나와
    기각됐다(memory: project_alt_rank_collecting). 순매수 상위는 "누가 사고 있나"
    라는 다른 축이라 실측 겹침이 4/30(13%)에 그치고, ETF/ETN 이 상위를 점유하지도
    않는다(probe 2026-08-09). 매매 경로는 건드리지 않고 A/B 판정용으로만 수집한다.

    `investor`: "foreign"(외국인) 이 사전 등록된 1차 가설, "institution"(기관계)는
    탐색용 2차 — 판정 규칙은 scripts/diagnostics/rank_signal_ab_study.py 참조.

    응답에 가격·누적거래량이 없어 `RankedSymbol.volume/price` 는 0 이다. 정렬·분석에
    쓸 값은 `metric`(순매수 수량, 부호 유지) 과 `rank`(시장 내 순위).
    """
    return await _merged_top(
        client,
        source=_net_buy_source(investor),
        top_n=top_n,
        markets=markets,
        exclude_etf=exclude_etf,
        exclude_symbols=exclude_symbols,
    )


__all__ = [
    "Market",
    "NetBuyInvestor",
    "RankSource",
    "RankedSymbol",
    "top_net_buy",
    "top_value",
    "top_volume",
]
