"""market.index 회귀 테스트.

`index_gate()` 가 (1) KOSPI/KOSDAQ 종합지수 코드를 inds_cd 로 보내는지
(2) 5/20 SMA 를 정확히 계산해 마지막 종가와 비교하는지
(3) 봉 부족 / API 실패 시 보수적으로 above=False 를 돌리는지 검증.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest
import respx

from auto_trading.config import KiwoomMode, Settings
from auto_trading.kiwoom.endpoints import (
    PATH_SECTOR,
    PATH_TOKEN,
    SECTOR_CODE_KOSDAQ,
    SECTOR_CODE_KOSPI,
    TR,
)
from auto_trading.kiwoom.rest import KiwoomRestClient
from auto_trading.market.index import SMA_LONG, SMA_SHORT, index_gate


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


def _payload(closes_newest_first: list[float]) -> dict:
    rows = [
        {"dt": f"2026{4:02d}{i:02d}", "cur_prc": f"{c:.2f}"}
        for i, c in enumerate(closes_newest_first, start=1)
    ]
    return {
        "return_code": 0,
        "return_msg": "정상",
        "inds_dt_pole": rows,  # extract_rows 가 첫 list[dict] 잡음
    }


async def test_index_gate_above_when_last_above_5_and_20(settings: Settings) -> None:
    # 마지막(최신) 종가 가장 큼 → 5/20 SMA 둘 다 위
    # closes[0]=last=300 ; SMA5=mean(300,290,280,270,260)=280 ; SMA20=mean(0..19) ≤ 280 < 300
    closes = [300.0 - i * 10 for i in range(SMA_LONG + 5)]  # 300, 290, ..., 늘어남
    posted_bodies: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        import json

        posted_bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_payload(closes))

    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        sect_route = mock.post(PATH_SECTOR).mock(side_effect=_handler)

        async with KiwoomRestClient(settings=settings) as client:
            gate = await index_gate(client, "KOSPI")

    assert sect_route.called
    assert sect_route.calls[0].request.headers["api-id"] == TR.SECTOR_INDEX_DAILY
    assert posted_bodies[0]["inds_cd"] == SECTOR_CODE_KOSPI
    assert gate.market == "KOSPI"
    assert gate.last == 300.0
    assert gate.sma5 == sum(closes[:SMA_SHORT]) / SMA_SHORT
    assert gate.sma20 == sum(closes[:SMA_LONG]) / SMA_LONG
    assert gate.above_5_and_20 is True
    assert gate.bars >= SMA_LONG


async def test_index_gate_below_when_last_under_sma(settings: Settings) -> None:
    # 마지막(최신) 종가가 가장 낮음 → SMA 아래
    closes = [100.0 + i * 10 for i in range(SMA_LONG + 5)]  # 100, 110, ... 과거가 더 큼
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        mock.post(PATH_SECTOR).mock(return_value=httpx.Response(200, json=_payload(closes)))
        async with KiwoomRestClient(settings=settings) as client:
            gate = await index_gate(client, "KOSDAQ")
    assert gate.last == 100.0
    assert gate.sma5 > gate.last
    assert gate.above_5_and_20 is False


async def test_index_gate_insufficient_bars_returns_false(settings: Settings) -> None:
    closes = [100.0, 110.0, 120.0]  # 20일 미만
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        mock.post(PATH_SECTOR).mock(return_value=httpx.Response(200, json=_payload(closes)))
        async with KiwoomRestClient(settings=settings) as client:
            gate = await index_gate(client, "KOSPI")
    assert gate.above_5_and_20 is False
    assert gate.bars == 3
    assert gate.sma20 == 0.0


async def test_index_gate_kosdaq_uses_correct_sector_code(settings: Settings) -> None:
    closes = [200.0] * (SMA_LONG + 1)
    posted: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        import json

        posted.append(json.loads(request.content))
        return httpx.Response(200, json=_payload(closes))

    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        mock.post(PATH_SECTOR).mock(side_effect=_handler)
        async with KiwoomRestClient(settings=settings) as client:
            await index_gate(client, "KOSDAQ")
    assert posted[0]["inds_cd"] == SECTOR_CODE_KOSDAQ


async def test_index_gate_api_failure_is_conservative_false(settings: Settings) -> None:
    """KiwoomError raise 되어도 게이트는 False 로 떨어져 매수 차단."""
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        mock.post(PATH_SECTOR).mock(
            return_value=httpx.Response(
                200,
                json={"return_code": 9, "return_msg": "지원하지 않는 API"},
            )
        )
        async with KiwoomRestClient(settings=settings) as client:
            gate = await index_gate(client, "KOSPI")
    assert gate.above_5_and_20 is False
    assert gate.bars == 0
