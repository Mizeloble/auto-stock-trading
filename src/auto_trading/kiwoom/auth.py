"""OAuth 토큰 발급/캐시.

키움 REST API 는 모든 요청 헤더에 `Authorization: Bearer <token>` 이 필요하다.
토큰은 대략 24시간 유효하며, 매 요청마다 발급하면 레이트리밋·불필요한 지연이 생기므로
메모리에 캐시하고 만료 임박 시 선제 재발급한다.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import httpx
import structlog

from ..config import Settings, get_settings
from .endpoints import PATH_TOKEN
from .schemas import KiwoomError, TokenResponse

log = structlog.get_logger(__name__)

# 만료 몇 초 전에 선제 재발급 — 요청 중 만료되어 401 맞는 사고 방지
REFRESH_MARGIN = timedelta(minutes=10)
# expires_at 을 파싱하지 못했을 때 사용하는 안전 기본값 (문서 권장: ~24h)
DEFAULT_TTL = timedelta(hours=23)


class TokenProvider:
    """토큰을 얻고 캐시한다. 여러 코루틴에서 공유해도 안전."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._client = client  # None 이면 요청 시점에 생성
        self._owns_client = client is None
        self._token: str | None = None
        self._expires_at: datetime | None = None
        self._lock = asyncio.Lock()

    async def get_token(self) -> str:
        async with self._lock:
            if (
                self._token
                and self._expires_at
                and datetime.now() < self._expires_at - REFRESH_MARGIN
            ):
                return self._token
            return await self._issue()

    async def invalidate(self) -> None:
        async with self._lock:
            self._token = None
            self._expires_at = None

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _issue(self) -> str:
        client = self._client or httpx.AsyncClient(
            base_url=self._settings.rest_base_url, timeout=10.0
        )
        if self._client is None:
            self._client = client

        payload: dict[str, Any] = {
            "grant_type": "client_credentials",
            "appkey": self._settings.kiwoom_app_key.get_secret_value(),
            "secretkey": self._settings.kiwoom_app_secret.get_secret_value(),
        }
        resp = await client.post(PATH_TOKEN, json=payload)
        if resp.status_code >= 400:
            raise KiwoomError(
                code=str(resp.status_code),
                message=f"token request failed: {resp.text[:200]}",
                payload={"status": resp.status_code},
            )
        data = resp.json()
        # HTTP 200 이라도 본문 return_code != 0 이면 비즈니스 에러 (예: 8030 모의/실전 키 불일치).
        # rest.py 의 동일 처리와 맞추어, ValidationError 대신 사람이 읽을 수 있는 메시지로 변환.
        rc = data.get("return_code")
        if rc is not None and rc != 0:
            raise KiwoomError(
                code=str(rc),
                message=str(data.get("return_msg") or "unknown"),
                payload={"path": PATH_TOKEN},
            )
        parsed = TokenResponse.model_validate(data)
        self._token = parsed.token
        self._expires_at = parsed.expires_at or (datetime.now() + DEFAULT_TTL)

        log.info(
            "kiwoom.token_issued",
            token_preview=_mask(parsed.token),
            expires_at=self._expires_at.isoformat() if self._expires_at else None,
            mode=self._settings.kiwoom_mode.value,
        )
        return self._token


def _mask(token: str) -> str:
    """로그에 찍을 안전한 프리뷰."""
    if not token:
        return "<empty>"
    return token[:4] + "…" + str(len(token))
