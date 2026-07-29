"""TokenProvider 단위 테스트."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

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


async def test_repeated_failures_do_not_hammer_token_endpoint(settings: Settings) -> None:
    """발급 실패 후 백오프 — 연속 호출이 네트워크를 다시 때리지 않는다 (2026-07-29 사고).

    rest.request() 가 매 요청마다 get_token() 을 부르므로 백오프가 없으면
    스냅샷 1라운드(200종목)가 곧바로 200회 발급 시도가 된다. 실제로 하루 2,009회를
    때려 키움 유량 제한(429)에 걸렸고, 그 429 가 진짜 원인(8001)을 덮었다.
    """
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        route = mock.post(PATH_TOKEN).mock(
            return_value=httpx.Response(
                200,
                json={
                    "return_code": 3,
                    "return_msg": "인증에 실패했습니다[8001:App Key와 Secret Key 검증에 실패했습니다]",
                },
            )
        )
        provider = TokenProvider(settings=settings)
        try:
            for _ in range(50):
                with pytest.raises(KiwoomError) as ei:
                    await provider.get_token()
                # 억제된 호출도 호출자에겐 동일한 오류로 보여야 한다(침묵 금지).
                assert "8001" in ei.value.message
        finally:
            await provider.aclose()
    # 50회 호출했지만 실제 발급 요청은 1회뿐.
    assert route.call_count == 1


async def test_backoff_clears_after_successful_issue(settings: Settings) -> None:
    """백오프 창이 지나 발급에 성공하면 실패 상태가 초기화된다."""
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=httpx.Response(500, text="boom"))
        provider = TokenProvider(settings=settings)
        try:
            with pytest.raises(KiwoomError):
                await provider.get_token()
            assert provider._retry_after is not None
            # 백오프 만료 시뮬 — 창이 지난 것으로 되돌린다.
            provider._retry_after = datetime.now() - timedelta(seconds=1)
            mock.post(PATH_TOKEN).mock(return_value=httpx.Response(200, json={"token": "ok-token"}))
            assert await provider.get_token() == "ok-token"
            assert provider._retry_after is None
            assert provider._issue_failures == 0
        finally:
            await provider.aclose()


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


# ---- 29단계 PR12 (#21) — 디스크 영속화 ---------------------------------------


async def test_token_saved_to_cache_after_issue(settings: Settings, tmp_path: Path) -> None:
    """발급 직후 cache_path 에 token + expires_at JSON 저장."""
    cache = tmp_path / "auth" / "token.json"
    expires_dt_str = _future(datetime.now() + timedelta(hours=24))

    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(
            return_value=httpx.Response(
                200, json={"token": "saved-token", "expires_dt": expires_dt_str}
            )
        )
        provider = TokenProvider(settings=settings, cache_path=cache)
        try:
            await provider.get_token()
        finally:
            await provider.aclose()

    assert cache.exists()
    data = json.loads(cache.read_text(encoding="utf-8"))
    assert data["token"] == "saved-token"
    # ISO 8601 expires_at — datetime.fromisoformat 으로 다시 파싱 가능.
    parsed = datetime.fromisoformat(data["expires_at"])
    assert parsed > datetime.now() + timedelta(hours=23)


async def test_token_loaded_from_cache_skips_http_call(settings: Settings, tmp_path: Path) -> None:
    """시동 시 디스크에 신선한 캐시 있으면 HTTP 발급 호출 안 함."""
    cache = tmp_path / "token.json"
    cache.write_text(
        json.dumps(
            {
                "token": "cached-token",
                "expires_at": (datetime.now() + timedelta(hours=20)).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    async with respx.mock(base_url=settings.rest_base_url, assert_all_called=False) as mock:
        route = mock.post(PATH_TOKEN).mock(return_value=httpx.Response(200, json={"token": "X"}))
        provider = TokenProvider(settings=settings, cache_path=cache)
        try:
            tok = await provider.get_token()
        finally:
            await provider.aclose()

    assert tok == "cached-token"
    assert route.call_count == 0  # 디스크 캐시로 회피


async def test_expired_cache_triggers_fresh_issue(settings: Settings, tmp_path: Path) -> None:
    """만료 마진 안의 캐시는 무시하고 새로 발급."""
    cache = tmp_path / "token.json"
    cache.write_text(
        json.dumps(
            {
                "token": "expired-token",
                # REFRESH_MARGIN(10분) 안 — 안 쓰고 새로 발급해야.
                "expires_at": (datetime.now() + timedelta(minutes=5)).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    async with respx.mock(base_url=settings.rest_base_url) as mock:
        route = mock.post(PATH_TOKEN).mock(
            return_value=httpx.Response(
                200,
                json={
                    "token": "fresh-token",
                    "expires_dt": _future(datetime.now() + timedelta(hours=24)),
                },
            )
        )
        provider = TokenProvider(settings=settings, cache_path=cache)
        try:
            tok = await provider.get_token()
        finally:
            await provider.aclose()

    assert tok == "fresh-token"
    assert route.call_count == 1


async def test_corrupt_cache_falls_back_to_fresh_issue(settings: Settings, tmp_path: Path) -> None:
    """손상된 캐시 (JSON 깨짐 / 필드 부족) 는 swallow 하고 정상 발급."""
    cache = tmp_path / "token.json"
    cache.write_text("not json at all", encoding="utf-8")

    async with respx.mock(base_url=settings.rest_base_url) as mock:
        route = mock.post(PATH_TOKEN).mock(
            return_value=httpx.Response(
                200,
                json={
                    "token": "fresh",
                    "expires_dt": _future(datetime.now() + timedelta(hours=12)),
                },
            )
        )
        provider = TokenProvider(settings=settings, cache_path=cache)
        try:
            tok = await provider.get_token()
        finally:
            await provider.aclose()

    assert tok == "fresh"
    assert route.call_count == 1


async def test_invalidate_deletes_disk_cache(settings: Settings, tmp_path: Path) -> None:
    cache = tmp_path / "token.json"
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(
            return_value=httpx.Response(
                200,
                json={"token": "t", "expires_dt": _future(datetime.now() + timedelta(hours=12))},
            )
        )
        provider = TokenProvider(settings=settings, cache_path=cache)
        try:
            await provider.get_token()
            assert cache.exists()
            await provider.invalidate()
        finally:
            await provider.aclose()

    assert not cache.exists()


async def test_cache_file_has_owner_only_permissions(settings: Settings, tmp_path: Path) -> None:
    """POSIX 환경에서 시크릿 파일 권한 0o600 강제."""
    cache = tmp_path / "token.json"
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(
            return_value=httpx.Response(
                200,
                json={"token": "t", "expires_dt": _future(datetime.now() + timedelta(hours=12))},
            )
        )
        provider = TokenProvider(settings=settings, cache_path=cache)
        try:
            await provider.get_token()
        finally:
            await provider.aclose()

    mode = os.stat(cache).st_mode & 0o777
    assert mode == 0o600


def test_settings_kiwoom_token_cache_path_default_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """env·.env 모두 미설정 시 default None — 디스크 영속화 비활성 (기존 동작 호환)."""
    monkeypatch.delenv("KIWOOM_TOKEN_CACHE_PATH", raising=False)
    s = Settings(kiwoom_app_key="k", kiwoom_app_secret="s", _env_file=None)
    assert s.kiwoom_token_cache_path is None


async def test_explicit_cache_path_overrides_settings(tmp_path: Path) -> None:
    """명시 인자가 settings 의 default 보다 우선."""
    explicit = tmp_path / "explicit.json"
    settings_path = tmp_path / "settings.json"
    s = Settings(
        kiwoom_mode=KiwoomMode.MOCK,
        kiwoom_app_key="k",
        kiwoom_app_secret="x",
        kiwoom_token_cache_path=settings_path,
    )

    async with respx.mock(base_url=s.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(
            return_value=httpx.Response(
                200,
                json={"token": "t", "expires_dt": _future(datetime.now() + timedelta(hours=12))},
            )
        )
        provider = TokenProvider(settings=s, cache_path=explicit)
        try:
            await provider.get_token()
        finally:
            await provider.aclose()

    assert explicit.exists()
    assert not settings_path.exists()
