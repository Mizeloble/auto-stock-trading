"""키움 REST API 공통 HTTP 클라이언트.

모든 TR 요청은 이 클래스의 `request(tr_id, path, body)` 를 통하게 해, 인증 헤더 주입
· 오류 매핑 · 페이지네이션 처리를 한 곳에 모은다.

키움 REST 규약(요지):
- 헤더: `Authorization: Bearer <token>`, `api-id: <TR>`, `cont-yn`, `next-key`
- 응답: JSON. 성공 시 `return_code == 0`, 실패 시 code/message 포함.
- 페이지네이션: 응답 헤더에 `cont-yn: "Y"`, `next-key: <cursor>` 가 있으면 다음 요청 헤더에 실어 보냄.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
import structlog

from ..config import Settings, get_settings
from .auth import TokenProvider
from .schemas import KiwoomError

log = structlog.get_logger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}

# 키움은 무효/만료 토큰을 HTTP 401 이 아니라 HTTP 200 + return_code 로 돌려준다.
# 관측된 형태: [3] 인증에 실패했습니다[8005:Token이 유효하지 않습니다]. 이 신호가 보이면
# 캐시 토큰을 버리고 한 번 재발급해 재시도 — 죽은 캐시 하나로 하루를 날리는 사고 방지.
_AUTH_ERROR_HINTS = ("8005", "8004", "인증에 실패", "유효하지 않")


def _is_token_auth_error(code: object, message: str) -> bool:
    text = f"{code} {message}"
    return any(hint in text for hint in _AUTH_ERROR_HINTS)


class KiwoomRestClient:
    """키움 REST 호출 래퍼. async with 로 사용 권장."""

    def __init__(
        self,
        settings: Settings | None = None,
        token_provider: TokenProvider | None = None,
        client: httpx.AsyncClient | None = None,
        max_retries: int = 3,
    ) -> None:
        self._settings = settings or get_settings()
        self._client = client or httpx.AsyncClient(
            base_url=self._settings.rest_base_url, timeout=10.0
        )
        self._owns_client = client is None
        self._token_provider = token_provider or TokenProvider(
            settings=self._settings, client=self._client
        )
        self._max_retries = max_retries

    @property
    def token_provider(self) -> TokenProvider:
        """동일 인증 컨텍스트를 공유하려는 WS 등 외부 컴포넌트용 접근자."""
        return self._token_provider

    async def __aenter__(self) -> KiwoomRestClient:
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._token_provider.aclose()
        if self._owns_client:
            await self._client.aclose()

    async def request(
        self,
        tr_id: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        cont_yn: str = "N",
        next_key: str = "",
    ) -> dict[str, Any]:
        """단일 페이지 요청. 429/5xx 는 지수 백오프로 재시도."""
        token = await self._token_provider.get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "api-id": tr_id,
            "cont-yn": cont_yn,
            "next-key": next_key,
            "Content-Type": "application/json;charset=UTF-8",
        }
        attempt = 0
        auth_retried = False
        while True:
            resp = await self._client.post(path, headers=headers, json=body or {})
            if resp.status_code == 401:
                # 토큰이 서버측 만료일 수 있음 — 한번만 재발급 시도
                await self._token_provider.invalidate()
                token = await self._token_provider.get_token()
                headers["Authorization"] = f"Bearer {token}"
                resp = await self._client.post(path, headers=headers, json=body or {})

            if resp.status_code in RETRY_STATUSES and attempt < self._max_retries:
                delay = 0.5 * (2**attempt)
                log.warning(
                    "kiwoom.retry",
                    status=resp.status_code,
                    tr_id=tr_id,
                    attempt=attempt + 1,
                    delay=delay,
                )
                await asyncio.sleep(delay)
                attempt += 1
                continue

            if resp.status_code >= 400:
                raise KiwoomError(
                    code=str(resp.status_code),
                    message=f"http error: {resp.text[:300]}",
                    payload={"tr_id": tr_id, "path": path},
                )

            data: dict[str, Any] = resp.json()
            rc = data.get("return_code")
            if rc is not None and rc != 0:
                msg = str(data.get("return_msg") or "unknown")
                if not auth_retried and _is_token_auth_error(rc, msg):
                    log.warning(
                        "kiwoom.token_invalid_reissue",
                        tr_id=tr_id,
                        code=rc,
                        message=msg,
                    )
                    await self._token_provider.invalidate()
                    token = await self._token_provider.get_token()
                    headers["Authorization"] = f"Bearer {token}"
                    auth_retried = True
                    continue
                raise KiwoomError(
                    code=str(rc),
                    message=msg,
                    payload={"tr_id": tr_id, "path": path},
                )

            # 페이지네이션 메타데이터는 헤더에서 꺼내서 응답 dict 에 실어준다
            data["_cont_yn"] = resp.headers.get("cont-yn", "N")
            data["_next_key"] = resp.headers.get("next-key", "")
            return data

    async def paginate(
        self,
        tr_id: str,
        path: str,
        body: dict[str, Any] | None = None,
        max_pages: int = 20,
    ) -> AsyncIterator[dict[str, Any]]:
        """`cont-yn=Y` 가 올 때까지 자동 페이지 순회."""
        cont_yn = "N"
        next_key = ""
        for _ in range(max_pages):
            page = await self.request(tr_id, path, body, cont_yn=cont_yn, next_key=next_key)
            yield page
            if page.get("_cont_yn") != "Y" or not page.get("_next_key"):
                return
            cont_yn = "Y"
            next_key = page["_next_key"]
