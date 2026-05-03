"""market.rank 회귀 테스트.

키움 모의 서버 응답 형태를 mock 으로 재현해 (1) 시장별 호출이 분리되는지
(2) 두 시장을 합쳐 거래량 desc 정렬되는지 (3) top_n 으로 잘리는지를 잡는다.
응답 키가 바뀌면 즉시 깨지도록 필드명을 명시.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest
import respx

from auto_trading.config import KiwoomMode, Settings
from auto_trading.kiwoom.endpoints import (
    MARKET_TP_KOSDAQ,
    MARKET_TP_KOSPI,
    PATH_RANK,
    PATH_TOKEN,
    TR,
)
from auto_trading.kiwoom.rest import KiwoomRestClient
from auto_trading.market.rank import top_volume


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


def _rank_payload(rows: list[dict]) -> dict:
    return {
        "return_code": 0,
        "return_msg": "정상",
        "trde_qty_upper": rows,  # 응답 list 키는 추출 로직이 첫 번째 list[dict] 를 잡음
    }


async def test_top_volume_merges_two_markets_and_sorts_by_volume(settings: Settings) -> None:
    kospi_rows = [
        {"stk_cd": "005930", "stk_nm": "삼성전자", "trde_qty": "30000000", "cur_prc": "70000"},
        {"stk_cd": "000660", "stk_nm": "SK하이닉스", "trde_qty": "10000000", "cur_prc": "150000"},
    ]
    kosdaq_rows = [
        {"stk_cd": "247540", "stk_nm": "에코프로비엠", "trde_qty": "25000000", "cur_prc": "200000"},
        {"stk_cd": "086520", "stk_nm": "에코프로", "trde_qty": "5000000", "cur_prc": "80000"},
    ]

    posted_bodies: list[dict] = []

    def _route_handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        posted_bodies.append(body)
        if body["mrkt_tp"] == MARKET_TP_KOSPI:
            return httpx.Response(200, json=_rank_payload(kospi_rows))
        if body["mrkt_tp"] == MARKET_TP_KOSDAQ:
            return httpx.Response(200, json=_rank_payload(kosdaq_rows))
        return httpx.Response(404, json={"return_code": 99})

    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        rank_route = mock.post(PATH_RANK).mock(side_effect=_route_handler)

        async with KiwoomRestClient(settings=settings) as client:
            chosen = await top_volume(client, top_n=3)

    # 두 시장 모두 호출했는가
    assert rank_route.call_count == 2
    sent_tps = sorted(b["mrkt_tp"] for b in posted_bodies)
    assert sent_tps == sorted([MARKET_TP_KOSPI, MARKET_TP_KOSDAQ])
    # api-id 가 RANK_VOLUME 인지 (TR 코드 회귀 게이트)
    for call in rank_route.calls:
        assert call.request.headers["api-id"] == TR.RANK_VOLUME

    assert len(chosen) == 3
    # 거래량 desc 정렬: 삼성전자(3000만) > 에코프로비엠(2500만) > SK하이닉스(1000만)
    assert [r.symbol for r in chosen] == ["005930", "247540", "000660"]
    # 시장 분류 보존
    markets = {r.symbol: r.market for r in chosen}
    assert markets["005930"] == "KOSPI"
    assert markets["247540"] == "KOSDAQ"
    # 가격·이름 파싱
    assert chosen[0].name == "삼성전자"
    assert chosen[0].price == 70000.0


async def test_top_volume_top_n_truncates(settings: Settings) -> None:
    rows = [
        {"stk_cd": f"00000{i}", "stk_nm": f"종목{i}", "trde_qty": str(1000 - i), "cur_prc": "100"}
        for i in range(10)
    ]
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        mock.post(PATH_RANK).mock(return_value=httpx.Response(200, json=_rank_payload(rows)))
        async with KiwoomRestClient(settings=settings) as client:
            chosen = await top_volume(client, top_n=2, markets=("KOSPI",))
    assert len(chosen) == 2
    assert chosen[0].volume > chosen[1].volume


async def test_top_volume_one_market_failure_does_not_block_other(settings: Settings) -> None:
    """KOSPI 응답이 깨져도 KOSDAQ 결과만으로 진행되어야 한다."""
    kosdaq_rows = [
        {"stk_cd": "247540", "stk_nm": "에코프로비엠", "trde_qty": "25000000", "cur_prc": "200000"},
    ]

    def _handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        if body["mrkt_tp"] == MARKET_TP_KOSPI:
            return httpx.Response(200, json={"return_code": 9, "return_msg": "지원하지 않는 API"})
        return httpx.Response(200, json=_rank_payload(kosdaq_rows))

    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        mock.post(PATH_RANK).mock(side_effect=_handler)
        async with KiwoomRestClient(settings=settings) as client:
            chosen = await top_volume(client, top_n=3)

    assert len(chosen) == 1
    assert chosen[0].symbol == "247540"
    assert chosen[0].market == "KOSDAQ"


async def test_top_volume_strips_a_prefix_from_symbol(settings: Settings) -> None:
    """일부 키움 응답이 'A005930' 처럼 prefix 를 붙임 — 도메인 객체에선 제거."""
    rows = [{"stk_cd": "A005930", "stk_nm": "삼성전자", "trde_qty": "1", "cur_prc": "70000"}]
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        mock.post(PATH_RANK).mock(return_value=httpx.Response(200, json=_rank_payload(rows)))
        async with KiwoomRestClient(settings=settings) as client:
            chosen = await top_volume(client, top_n=1, markets=("KOSPI",))
    assert chosen[0].symbol == "005930"


async def test_top_volume_strips_underscore_suffix_from_symbol(settings: Settings) -> None:
    """probe 2026-05-03 회귀 — 통합거래소(AL) 종목은 '252670_AL' 형태로 옴.

    주문 API 는 6자리 숫자만 받기 때문에 발굴기 단에서 _xx 접미사 제거 필수.
    """
    rows = [
        {
            "stk_cd": "252670_AL",
            "stk_nm": "KODEX 200선물인버스2X",
            "trde_qty": "10",
            "cur_prc": "+169",
        },
        {"stk_cd": "114800_AL", "stk_nm": "KODEX 인버스", "trde_qty": "5", "cur_prc": "+1349"},
    ]
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        mock.post(PATH_RANK).mock(return_value=httpx.Response(200, json=_rank_payload(rows)))
        async with KiwoomRestClient(settings=settings) as client:
            chosen = await top_volume(client, top_n=2, markets=("KOSPI",))
    assert [r.symbol for r in chosen] == ["252670", "114800"]
    # 가격 부호(`+169`) 는 to_decimal 이 처리.
    assert chosen[0].price == 169.0


async def test_top_volume_sends_all_required_body_fields(settings: Settings) -> None:
    """ka10030 필수 필드 회귀 게이트 — probe 2026-05-03 에서 누락이 1511 에러였음.

    crd_tp / trde_qty_tp / pric_tp / trde_prica_tp / mrkt_open_tp 가 빠지면
    키움이 즉시 거절. 이 필드 중 하나라도 사라지면 이 테스트가 실패해야 함.
    """
    captured: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.append(json.loads(request.content))
        return httpx.Response(200, json=_rank_payload([]))

    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        mock.post(PATH_RANK).mock(side_effect=_handler)
        async with KiwoomRestClient(settings=settings) as client:
            await top_volume(client, top_n=1, markets=("KOSPI",))

    assert captured, "POST 가 한 번도 안 갔음"
    body = captured[0]
    required = {
        "mrkt_tp",
        "sort_tp",
        "mang_stk_incls",
        "crd_tp",
        "trde_qty_tp",
        "pric_tp",
        "trde_prica_tp",
        "mrkt_open_tp",
        "stex_tp",
    }
    missing = required - set(body)
    assert not missing, f"필수 필드 누락: {missing} (probe 2026-05-03 에러 회귀)"
