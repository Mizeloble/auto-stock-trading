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


async def test_index_gate_sends_required_mrkt_tp(settings: Settings) -> None:
    """probe 2026-05-03 회귀 — ka20009 는 inds_cd 외 mrkt_tp 도 필수.

    KOSPI=0, KOSDAQ=1 로 시장별 분기. 빠지면 키움이 1511 로 즉시 거절.
    """
    from auto_trading.kiwoom.endpoints import INDEX_MRKT_TP_KOSDAQ, INDEX_MRKT_TP_KOSPI

    closes = [100.0] * (SMA_LONG + 1)
    posted: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        import json

        posted.append(json.loads(request.content))
        return httpx.Response(200, json=_payload(closes))

    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        mock.post(PATH_SECTOR).mock(side_effect=_handler)
        async with KiwoomRestClient(settings=settings) as client:
            await index_gate(client, "KOSPI")
            await index_gate(client, "KOSDAQ")

    assert "mrkt_tp" in posted[0], "KOSPI 호출에 mrkt_tp 누락"
    assert posted[0]["mrkt_tp"] == INDEX_MRKT_TP_KOSPI
    assert "mrkt_tp" in posted[1], "KOSDAQ 호출에 mrkt_tp 누락"
    assert posted[1]["mrkt_tp"] == INDEX_MRKT_TP_KOSDAQ
    # base_dt 도 같이 있어야 (probe 1차에서 빠지진 않았지만 회귀 게이트로)
    assert "base_dt" in posted[0]


async def test_index_gate_parses_cur_prc_n_from_ka20009_response(settings: Settings) -> None:
    """probe 2026-05-03 회귀 — ka20009 의 list `inds_cur_prc_daly_rept` 행 키는
    `cur_prc_n`(_n suffix). _parse_close 후보 체인에서 빠지면 게이트가 데이터 부족으로
    무조건 False — 모든 종목 매수 차단됨.
    """
    # 실측 응답을 단순화: cur_prc_n 만 있는 행 20건, 단조 증가(마지막이 가장 큼).
    rows = [
        {
            "dt_n": f"2026{4:02d}{i + 1:02d}",
            "cur_prc_n": f"{1000.0 + (SMA_LONG - i) * 10:.2f}",  # 최신=마지막=1200
        }
        for i in range(SMA_LONG)
    ]
    payload = {
        "return_code": 0,
        "return_msg": "정상",
        "cur_prc": "+6598.87",  # top-level 필드(현재가) — list 가 아니라 무시되어야
        "inds_cur_prc_daly_rept": rows,
    }
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        mock.post(PATH_SECTOR).mock(return_value=httpx.Response(200, json=payload))
        async with KiwoomRestClient(settings=settings) as client:
            gate = await index_gate(client, "KOSPI")

    # cur_prc_n 매핑이 작동하면 last 종가가 list 첫 행 값(1200) 이어야.
    assert gate.last == 1200.0, (
        f"cur_prc_n 매핑 실패 — last={gate.last} (top-level cur_prc 잡았거나 0). "
        "_parse_close 의 후보 체인에서 cur_prc_n 가 빠진 게 원인일 수 있음."
    )
    assert gate.bars == SMA_LONG


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
