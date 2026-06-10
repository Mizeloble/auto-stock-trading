"""KiwoomRestClient 단위 테스트."""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest
import respx

from auto_trading.config import KiwoomMode, Settings
from auto_trading.kiwoom.endpoints import PATH_MARKET, PATH_TOKEN, TR
from auto_trading.kiwoom.rest import KiwoomRestClient
from auto_trading.kiwoom.schemas import KiwoomError


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


async def test_request_injects_headers_and_unwraps_success(settings: Settings) -> None:
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        route = mock.post(PATH_MARKET).mock(
            return_value=httpx.Response(200, json={"return_code": 0, "cur_prc": "70000"})
        )

        async with KiwoomRestClient(settings=settings) as client:
            data = await client.request(TR.QUOTE_CURRENT, PATH_MARKET, {"stk_cd": "005930"})

        assert data["cur_prc"] == "70000"
        sent = route.calls[0].request
        assert sent.headers["authorization"] == "Bearer tkn"
        assert sent.headers["api-id"] == TR.QUOTE_CURRENT
        assert sent.headers["cont-yn"] == "N"


async def test_return_code_nonzero_raises(settings: Settings) -> None:
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        mock.post(PATH_MARKET).mock(
            return_value=httpx.Response(200, json={"return_code": 900, "return_msg": "bad symbol"})
        )
        async with KiwoomRestClient(settings=settings) as client:
            with pytest.raises(KiwoomError) as exc:
                await client.request(TR.QUOTE_CURRENT, PATH_MARKET, {"stk_cd": "X"})
        assert exc.value.code == "900"


async def test_retry_on_429(settings: Settings) -> None:
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        route = mock.post(PATH_MARKET).mock(
            side_effect=[
                httpx.Response(429, text="rate limited"),
                httpx.Response(200, json={"return_code": 0, "cur_prc": "1"}),
            ]
        )
        async with KiwoomRestClient(settings=settings, max_retries=2) as client:
            data = await client.request(TR.QUOTE_CURRENT, PATH_MARKET, {"stk_cd": "A"})
        assert data["cur_prc"] == "1"
        assert route.call_count == 2


async def test_401_triggers_token_refresh(settings: Settings) -> None:
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(side_effect=[_token_resp(), _token_resp()])
        mock.post(PATH_MARKET).mock(
            side_effect=[
                httpx.Response(401, text="expired"),
                httpx.Response(200, json={"return_code": 0, "cur_prc": "5"}),
            ]
        )
        async with KiwoomRestClient(settings=settings) as client:
            data = await client.request(TR.QUOTE_CURRENT, PATH_MARKET, {"stk_cd": "A"})
        assert data["cur_prc"] == "5"


async def test_body_level_invalid_token_triggers_reissue(settings: Settings) -> None:
    """키움이 무효 토큰을 HTTP 200 + return_code 로 돌려줄 때(8005) 한 번 재발급·재시도.

    죽은 캐시 토큰 하나로 발굴이 통째로 비어 하루를 날린 사고(2026-05-15) 회귀 방지.
    """
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(side_effect=[_token_resp(), _token_resp()])
        mock.post(PATH_MARKET).mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        "return_code": 3,
                        "return_msg": "인증에 실패했습니다[8005:Token이 유효하지 않습니다]",
                    },
                ),
                httpx.Response(200, json={"return_code": 0, "cur_prc": "9"}),
            ]
        )
        async with KiwoomRestClient(settings=settings) as client:
            data = await client.request(TR.QUOTE_CURRENT, PATH_MARKET, {"stk_cd": "A"})
        assert data["cur_prc"] == "9"


async def test_body_level_invalid_token_reissue_only_once(settings: Settings) -> None:
    """재발급 후에도 8005 면 무한루프 없이 KiwoomError 로 올린다."""
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(side_effect=[_token_resp(), _token_resp()])
        mock.post(PATH_MARKET).mock(
            return_value=httpx.Response(
                200,
                json={"return_code": 3, "return_msg": "[8005:Token이 유효하지 않습니다]"},
            )
        )
        async with KiwoomRestClient(settings=settings) as client:
            with pytest.raises(KiwoomError) as exc:
                await client.request(TR.QUOTE_CURRENT, PATH_MARKET, {"stk_cd": "A"})
        assert exc.value.code == "3"


async def test_paginate_follows_cont_yn(settings: Settings) -> None:
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        mock.post(PATH_MARKET).mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={"return_code": 0, "rows": [{"cur_prc": "1"}]},
                    headers={"cont-yn": "Y", "next-key": "k1"},
                ),
                httpx.Response(
                    200,
                    json={"return_code": 0, "rows": [{"cur_prc": "2"}]},
                    headers={"cont-yn": "N", "next-key": ""},
                ),
            ]
        )
        async with KiwoomRestClient(settings=settings) as client:
            pages = [p async for p in client.paginate(TR.QUOTE_CURRENT, PATH_MARKET, {})]

        assert len(pages) == 2
        assert pages[0]["rows"][0]["cur_prc"] == "1"
        assert pages[1]["rows"][0]["cur_prc"] == "2"


# 2026-05-16 관측 — 시작 직후 ka10081 버스트 429. 클라이언트측 선제 페이싱으로 차단.


async def test_pacing_spaces_consecutive_requests(settings: Settings) -> None:
    """min_request_interval_sec 설정 시 연속 요청이 최소 간격만큼 떨어진다."""
    import asyncio

    interval = 0.05
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        mock.post(PATH_MARKET).mock(
            return_value=httpx.Response(200, json={"return_code": 0, "cur_prc": "1"})
        )
        async with KiwoomRestClient(settings=settings, min_request_interval_sec=interval) as client:
            loop = asyncio.get_running_loop()
            await client.request(TR.QUOTE_CURRENT, PATH_MARKET, {})
            t0 = loop.time()
            await client.request(TR.QUOTE_CURRENT, PATH_MARKET, {})
            await client.request(TR.QUOTE_CURRENT, PATH_MARKET, {})
        # 2번째+3번째 사이 최소 1 interval 이상 (페이싱 누적).
        assert loop.time() - t0 >= interval


# ---- 2026-06-10 제3자 검토 — 주문 TR 의 5xx 자동 재시도 = 이중 체결 위험 ------


async def test_non_idempotent_5xx_raises_without_retry(settings: Settings) -> None:
    """idempotent=False(주문)는 5xx 를 재전송하지 않고 즉시 예외 — 이중 체결 방지."""
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        route = mock.post(PATH_MARKET).mock(
            side_effect=[
                httpx.Response(502, text="bad gateway"),
                httpx.Response(200, json={"return_code": 0}),  # 재시도하면 이게 잡힘
            ]
        )
        async with KiwoomRestClient(settings=settings, max_retries=3) as client:
            with pytest.raises(KiwoomError) as exc:
                await client.request(TR.QUOTE_CURRENT, PATH_MARKET, {}, idempotent=False)
    assert exc.value.code == "502"
    assert route.call_count == 1  # 단 1회 송신 — 자동 재전송 없음


async def test_non_idempotent_429_still_retried(settings: Settings) -> None:
    """429 는 처리 전 거절이 확실 — 주문이라도 재시도 허용."""
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        route = mock.post(PATH_MARKET).mock(
            side_effect=[
                httpx.Response(429, text="rate limited"),
                httpx.Response(200, json={"return_code": 0, "ord_no": "OK"}),
            ]
        )
        async with KiwoomRestClient(settings=settings, max_retries=2) as client:
            data = await client.request(TR.QUOTE_CURRENT, PATH_MARKET, {}, idempotent=False)
    assert data["ord_no"] == "OK"
    assert route.call_count == 2


async def test_idempotent_default_retries_5xx(settings: Settings) -> None:
    """조회 TR(기본 idempotent=True)은 기존대로 5xx 백오프 재시도."""
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        route = mock.post(PATH_MARKET).mock(
            side_effect=[
                httpx.Response(500, text="oops"),
                httpx.Response(200, json={"return_code": 0, "cur_prc": "3"}),
            ]
        )
        async with KiwoomRestClient(settings=settings, max_retries=2) as client:
            data = await client.request(TR.QUOTE_CURRENT, PATH_MARKET, {})
    assert data["cur_prc"] == "3"
    assert route.call_count == 2


async def test_pacing_disabled_when_interval_zero(settings: Settings) -> None:
    """간격 0 이면 페이싱 no-op — 기존 빠른 경로/테스트 호환."""
    import asyncio

    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        mock.post(PATH_MARKET).mock(
            return_value=httpx.Response(200, json={"return_code": 0, "cur_prc": "1"})
        )
        async with KiwoomRestClient(settings=settings, min_request_interval_sec=0.0) as client:
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            for _ in range(5):
                await client.request(TR.QUOTE_CURRENT, PATH_MARKET, {})
        assert loop.time() - t0 < 0.05  # 5회 즉시 — 인위적 지연 없음
