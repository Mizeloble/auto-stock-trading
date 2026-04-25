"""market.chart 회귀 테스트.

키움 mock 서버에서 확인한 실제 응답 형태 — TR `ka10081` 가 path `/api/dostk/chart`
에서 `stk_dt_pole_chart_qry` 리스트(필드: `dt`, `open_pric`, `high_pric`, `low_pric`,
`cur_prc`, `trde_qty`)를 반환하는 것을 mock 으로 재현해, 엔드포인트나 필드명이
바뀌어도 즉시 깨지도록 잡아둔다.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import httpx
import pytest
import respx

from auto_trading.config import KiwoomMode, Settings
from auto_trading.kiwoom.endpoints import PATH_CHART, PATH_TOKEN, TR
from auto_trading.kiwoom.rest import KiwoomRestClient
from auto_trading.kiwoom.schemas import KiwoomError
from auto_trading.market.chart import get_daily_bars


@pytest.fixture
def settings() -> Settings:
    return Settings(
        kiwoom_mode=KiwoomMode.MOCK,
        kiwoom_app_key="k",
        kiwoom_app_secret="s",
    )


def _token_resp() -> httpx.Response:
    exp = (datetime.now() + timedelta(hours=24)).strftime("%Y%m%d%H%M%S")
    return httpx.Response(200, json={"token": "tkn", "expires_dt": exp})


async def test_get_daily_bars_uses_chart_path_and_parses_rows(settings: Settings) -> None:
    payload = {
        "return_code": 0,
        "return_msg": "정상적으로 처리되었습니다",
        "stk_cd": "005930",
        "stk_dt_pole_chart_qry": [
            {
                "dt": "20260424",
                "open_pric": "224000",
                "high_pric": "225000",
                "low_pric": "216500",
                "cur_prc": "219500",
                "trde_qty": "19626666",
            },
            {
                "dt": "20260423",
                "open_pric": "223000",
                "high_pric": "229500",
                "low_pric": "216000",
                "cur_prc": "224500",
                "trde_qty": "34525485",
            },
        ],
    }
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        chart_route = mock.post(PATH_CHART).mock(return_value=httpx.Response(200, json=payload))

        async with KiwoomRestClient(settings=settings) as client:
            bars = await get_daily_bars(client, "005930", base_dt=date(2026, 4, 24))

    assert chart_route.called
    sent = chart_route.calls[0].request
    assert sent.headers["api-id"] == TR.CHART_DAILY  # ka10081 - 다른 TR 로 교체되면 즉시 실패
    assert len(bars) == 2
    first = bars[0]
    assert first.symbol == "005930"
    assert first.ts.date() == date(2026, 4, 24)
    assert first.open == 224000.0
    assert first.high == 225000.0
    assert first.low == 216500.0
    assert first.close == 219500.0  # cur_prc → close 매핑 회귀 방지
    assert first.volume == 19626666


async def test_get_daily_bars_raises_on_unsupported_uri(settings: Settings) -> None:
    """경로/TR 불일치를 키움이 거부할 때 KiwoomError 로 surface 되는지."""
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        mock.post(PATH_CHART).mock(
            return_value=httpx.Response(
                200,
                json={
                    "return_code": 9,
                    "return_msg": "해당 URI에서는 지원하는 API ID가 아닙니다",
                },
            )
        )
        async with KiwoomRestClient(settings=settings) as client:
            with pytest.raises(KiwoomError) as exc:
                await get_daily_bars(client, "005930")

    assert exc.value.code == "9"
    assert "지원하는 API ID" in exc.value.message
