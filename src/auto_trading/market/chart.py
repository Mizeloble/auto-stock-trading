"""일/분봉 차트 조회."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

import pandas as pd

from .._common import Bar, extract_rows, parse_kiwoom_ts, to_decimal
from ..kiwoom.endpoints import PATH_CHART, TR
from ..kiwoom.rest import KiwoomRestClient


async def get_daily_bars(
    client: KiwoomRestClient,
    symbol: str,
    *,
    base_dt: date | None = None,
    upd_stkpc_tp: str = "1",  # 수정주가 적용
    max_pages: int = 10,
) -> list[Bar]:
    """일봉 차트 (TR ka10081). 최신부터 과거 순으로 페이지네이션."""
    body = {
        "stk_cd": symbol,
        "base_dt": (base_dt or date.today()).strftime("%Y%m%d"),
        "upd_stkpc_tp": upd_stkpc_tp,
    }
    bars: list[Bar] = []
    async for page in client.paginate(TR.CHART_DAILY, PATH_CHART, body, max_pages=max_pages):
        for row in extract_rows(page):
            ts_raw = row.get("dt") or row.get("stck_bsop_date") or row.get("date")
            if not ts_raw:
                continue
            bars.append(
                Bar(
                    symbol=symbol,
                    ts=parse_kiwoom_ts(ts_raw),
                    open=float(
                        abs(
                            to_decimal(
                                row.get("open_pric") or row.get("stck_oprc") or row.get("open")
                            )
                        )
                    ),
                    high=float(
                        abs(
                            to_decimal(
                                row.get("high_pric") or row.get("stck_hgpr") or row.get("high")
                            )
                        )
                    ),
                    low=float(
                        abs(
                            to_decimal(
                                row.get("low_pric") or row.get("stck_lwpr") or row.get("low")
                            )
                        )
                    ),
                    close=float(
                        abs(
                            to_decimal(
                                row.get("cur_prc") or row.get("stck_clpr") or row.get("close")
                            )
                        )
                    ),
                    volume=int(
                        to_decimal(row.get("trde_qty") or row.get("acml_vol") or row.get("volume"))
                    ),
                )
            )
    return bars


async def get_minute_bars(
    client: KiwoomRestClient,
    symbol: str,
    *,
    tic_scope: str = "1",  # 1=1분봉
    on_date: date | None = None,
    upd_stkpc_tp: str = "1",  # 수정주가 적용
    max_pages: int = 3,
) -> list[Bar]:
    """분봉 차트 (TR ka10080). 최신부터 과거 순으로 페이지네이션 후 오름차순 반환.

    무엇/왜: 게이트 차단일 대안 발굴 축(순매수 등)의 픽을 **실제 출구 스택으로**
    사후 재생하려면 분 단위 가격이 필요하다. 이 축들은 러너 구독 유니버스가 아니라
    틱·봉이 실시간으로 쌓이지 않으므로 사후 조회가 유일한 경로다(2026-08-10).

    `on_date` 를 주면 그 날짜 봉만 남긴다 — 한 페이지(900봉)가 여러 거래일에 걸쳐
    오므로 필터가 없으면 다른 날 봉이 섞인다.

    **조회 범위는 `max_pages` 가 결정한다(2026-08-15 실측 정정).** 예전 주석은 "과거
    며칠까지만" 이라고 적었으나 실제로는 페이지당 900봉 ≈ 2.4거래일로 **선형 확장**된다 —
    005930 기준 5페이지=12거래일, 15페이지=36거래일, **40페이지=95거래일**(2026-03-30~
    08-14)까지 벽 없이 조회됐다. 기본값 3 은 얕은 용도(직전 며칠 재생)에 맞춘 것이므로,
    과거 표본이 필요하면 호출자가 `max_pages` 를 올려라(페이지마다 1 요청 — 페이싱 주의).
    그래도 조회되지 않는 오래된 일자는 결손이므로, 호출자는 빈 리스트를 "그날 데이터
    없음" 으로 다뤄야 한다.

    어떻게 확인: tests/test_market_chart.py 의 get_minute_bars 케이스.
    """
    body = {
        "stk_cd": symbol,
        "tic_scope": tic_scope,
        "upd_stkpc_tp": upd_stkpc_tp,
    }
    bars: list[Bar] = []
    async for page in client.paginate(TR.CHART_MINUTE, PATH_CHART, body, max_pages=max_pages):
        for row in extract_rows(page):
            # 분봉 체결시각은 `cntr_tm`(YYYYMMDDHHMMSS) — 일봉의 `dt` 와 키가 다르다.
            ts_raw = row.get("cntr_tm") or row.get("dt") or row.get("date")
            if not ts_raw:
                continue
            ts = parse_kiwoom_ts(ts_raw)
            if on_date is not None and ts.date() != on_date:
                continue
            bars.append(
                Bar(
                    symbol=symbol,
                    ts=ts,
                    open=float(abs(to_decimal(row.get("open_pric") or row.get("open")))),
                    high=float(abs(to_decimal(row.get("high_pric") or row.get("high")))),
                    low=float(abs(to_decimal(row.get("low_pric") or row.get("low")))),
                    close=float(abs(to_decimal(row.get("cur_prc") or row.get("close")))),
                    volume=int(to_decimal(row.get("trde_qty") or row.get("volume"))),
                )
            )
    bars.sort(key=lambda b: b.ts)
    return bars


def bars_to_dataframe(bars: Iterable[Bar]) -> pd.DataFrame:
    """백테스트·지표 계산용 DataFrame. 오름차순 ts 인덱스."""
    records = [
        {
            "ts": b.ts,
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
        }
        for b in bars
    ]
    if not records:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    return pd.DataFrame(records).sort_values("ts").set_index("ts")
