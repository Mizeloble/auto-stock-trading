"""일/분봉 차트 조회."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pandas as pd

from ..kiwoom.endpoints import PATH_CHART, TR
from ..kiwoom.rest import KiwoomRestClient

KST = ZoneInfo("Asia/Seoul")


@dataclass(frozen=True)
class Bar:
    symbol: str
    ts: datetime        # KST
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


def _d(raw: object) -> Decimal:
    if raw is None:
        return Decimal(0)
    s = str(raw).replace(",", "").replace("+", "").strip()
    if s in ("", "-"):
        return Decimal(0)
    try:
        return abs(Decimal(s))
    except Exception:
        return Decimal(0)


def _parse_date(raw: str) -> datetime:
    raw = str(raw)
    if len(raw) == 8:
        return datetime.strptime(raw, "%Y%m%d").replace(tzinfo=KST)
    if len(raw) == 14:
        return datetime.strptime(raw, "%Y%m%d%H%M%S").replace(tzinfo=KST)
    return datetime.fromisoformat(raw).astimezone(KST)


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
        rows = _extract_rows(page)
        for row in rows:
            ts_raw = row.get("dt") or row.get("stck_bsop_date") or row.get("date")
            if not ts_raw:
                continue
            bars.append(
                Bar(
                    symbol=symbol,
                    ts=_parse_date(ts_raw),
                    open=_d(row.get("open_pric") or row.get("stck_oprc") or row.get("open")),
                    high=_d(row.get("high_pric") or row.get("stck_hgpr") or row.get("high")),
                    low=_d(row.get("low_pric") or row.get("stck_lwpr") or row.get("low")),
                    close=_d(row.get("cur_prc") or row.get("stck_clpr") or row.get("close")),
                    volume=int(_d(row.get("trde_qty") or row.get("acml_vol") or row.get("volume"))),
                )
            )
    return bars


def _extract_rows(page: dict) -> list[dict]:
    """응답 dict 안에서 실제 데이터 리스트를 찾아낸다 (키 이름이 TR마다 다름)."""
    for key, value in page.items():
        if key.startswith("_"):
            continue
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value
    return []


def bars_to_dataframe(bars: list[Bar]) -> pd.DataFrame:
    """백테스트·지표 계산용 DataFrame. 오름차순 ts 인덱스."""
    if not bars:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(
        [
            {
                "ts": b.ts,
                "open": float(b.open),
                "high": float(b.high),
                "low": float(b.low),
                "close": float(b.close),
                "volume": b.volume,
            }
            for b in bars
        ]
    )
    df = df.sort_values("ts").set_index("ts")
    return df
