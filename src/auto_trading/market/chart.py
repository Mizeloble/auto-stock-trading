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
