"""TokenProvider 단위 테스트."""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest
import respx

from auto_trading.config import KiwoomMode, Settings
from auto_trading.kiwoom import auth as auth_mod
from auto_trading.kiwoom.auth import TokenProvider
from auto_trading.kiwoom.endpoints import PATH_TOKEN
from auto_trading.kiwoom.schemas import KiwoomError


@pytest.fixture
def settings() -> Settings:
    return Settings(
        kiwoom_mode=KiwoomMode.MOCK,
        kiwoom_app_key="test-key",
        kiwoom_app_secret="test-secret",
    )


def _future(dt: datetime) -> str:
    return dt.strftime("%Y%m%d%H%M%S")


async def test_token_issued_and_cached(settings: Settings) -> None:
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        route = mock.post(PATH_TOKEN).mock(
            return_value=httpx.Response(
                200,
                json={
                    "token": "abc123xyz",
                    "token_type": "bearer",
                    "expires_dt": _future(datetime.now() + timedelta(hours=24)),
                },
            )
        )
        provider = TokenProvider(settings=settings)
        try:
            t1 = await provider.get_token()
            t2 = await provider.get_token()
        finally:
            await provider.aclose()

    assert t1 == "abc123xyz"
    assert t2 == "abc123xyz"
    assert route.call_count == 1  # 캐시 히트


async def test_token_refreshed_near_expiry(settings: Settings, monkeypatch) -> None:
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        "token": "first",
                        "expires_dt": _future(datetime.now() + timedelta(hours=24)),
                    },
                ),
                httpx.Response(
                    200,
                    json={
                        "token": "second",
                        "expires_dt": _future(datetime.now() + timedelta(hours=24)),
                    },
                ),
            ]
        )
        provider = TokenProvider(settings=settings)
        try:
            t1 = await provider.get_token()
            # 토큰 만료를 과거로 조작해 재발급 강제
            provider._expires_at = datetime.now() - timedelta(seconds=1)
            t2 = await provider.get_token()
        finally:
            await provider.aclose()

    assert t1 == "first"
    assert t2 == "second"


async def test_token_http_error_raises(settings: Settings) -> None:
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=httpx.Response(401, text="invalid appkey"))
        provider = TokenProvider(settings=settings)
        try:
            with pytest.raises(KiwoomError):
                await provider.get_token()
        finally:
            await provider.aclose()


async def test_token_business_error_raises_with_return_msg(settings: Settings) -> None:
    """HTTP 200 + return_code≠0 (예: 8030 모의/실전 키 불일치) 케이스."""
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(
            return_value=httpx.Response(
                200,
                json={
                    "return_code": 2,
                    "return_msg": "입력 값 오류입니다[8030:투자구분(실전/모의)이 달라서 Appkey를 사용할수가 없습니다]",
                },
            )
        )
        provider = TokenProvider(settings=settings)
        try:
            with pytest.raises(KiwoomError) as ei:
                await provider.get_token()
        finally:
            await provider.aclose()
    assert ei.value.code == "2"
    assert "8030" in ei.value.message


async def test_default_ttl_used_when_expires_missing(settings: Settings) -> None:
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=httpx.Response(200, json={"token": "t"}))
        provider = TokenProvider(settings=settings)
        try:
            await provider.get_token()
            assert provider._expires_at is not None
            # 약 23시간 범위 — DEFAULT_TTL
            delta = provider._expires_at - datetime.now()
            assert timedelta(hours=22) < delta <= auth_mod.DEFAULT_TTL + timedelta(seconds=1)
        finally:
            await provider.aclose()
