"""OAuth 토큰 발급/캐시.

키움 REST API 는 모든 요청 헤더에 `Authorization: Bearer <token>` 이 필요하다.
토큰은 대략 24시간 유효하며, 매 요청마다 발급하면 레이트리밋·불필요한 지연이 생기므로
메모리에 캐시하고 만료 임박 시 선제 재발급한다.

29단계 PR12 (#21) — 메모리 캐시는 봇 재시작 시 휘발. 디스크 영속화로
재시작 비용 0 (24h TTL 활용). 키움 au10001 rate limit 회피.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import structlog

from .._common import mask_secret
from ..config import Settings, get_settings
from .endpoints import PATH_TOKEN
from .schemas import KiwoomError, TokenResponse

log = structlog.get_logger(__name__)

# 만료 몇 초 전에 선제 재발급 — 요청 중 만료되어 401 맞는 사고 방지
REFRESH_MARGIN = timedelta(minutes=10)
# expires_at 을 파싱하지 못했을 때 사용하는 안전 기본값 (문서 권장: ~24h)
DEFAULT_TTL = timedelta(hours=23)
# 발급 실패 후 재시도 억제 (2026-07-29 사고). rest.request() 가 매 요청마다
# get_token() 을 부르므로, 실패 시 그대로 재발급을 때리면 스냅샷 1라운드(200종목)가
# 곧바로 200회 발급 시도가 된다 — 실제로 하루 2,009회를 때려 키움 유량 제한(au10001)에
# 걸렸고, 그 429 가 진짜 원인(8001 AppKey 검증 실패)을 3일간 덮었다.
TOKEN_RETRY_BACKOFF_BASE = timedelta(seconds=5)
TOKEN_RETRY_BACKOFF_MAX = timedelta(minutes=5)


class TokenProvider:
    """토큰을 얻고 캐시한다. 여러 코루틴에서 공유해도 안전.

    29단계 PR12 — `cache_path` 가 주어지면 (또는 settings 의 default) 디스크
    영속화 활성: 시동 시 자동 로드, 발급 후 자동 저장, invalidate 시 자동 삭제.
    파일 권한 0o600 (시크릿이라 소유자만 읽기/쓰기). POSIX 만 적용.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
        *,
        cache_path: Path | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._client = client  # None 이면 요청 시점에 생성
        self._owns_client = client is None
        # 명시 인자 > Settings default. None 이면 디스크 영속화 비활성.
        self._cache_path = cache_path or self._settings.kiwoom_token_cache_path
        self._token: str | None = None
        self._expires_at: datetime | None = None
        self._lock = asyncio.Lock()
        # 발급 실패 백오프 — 연속 실패 수 / 다음 시도 가능 시각 / 마지막 오류.
        self._issue_failures = 0
        self._retry_after: datetime | None = None
        self._last_issue_error: Exception | None = None
        # 시동 시 디스크에서 로드 시도. 실패는 swallow — 정상 발급으로 fallback.
        self._load_cache()

    async def get_token(self) -> str:
        async with self._lock:
            if (
                self._token
                and self._expires_at
                and datetime.now() < self._expires_at - REFRESH_MARGIN
            ):
                return self._token
            # 백오프 중이면 네트워크를 때리지 않고 직전 오류를 그대로 올린다 —
            # 호출자 입장에선 동작 동일(발급 실패 예외), 키움 유량만 아낀다.
            if (
                self._retry_after is not None
                and self._last_issue_error is not None
                and datetime.now() < self._retry_after
            ):
                raise self._last_issue_error
            try:
                token = await self._issue()
            except Exception as exc:
                self._note_issue_failure(exc)
                raise
            self._issue_failures = 0
            self._retry_after = None
            self._last_issue_error = None
            return token

    def _note_issue_failure(self, exc: Exception) -> None:
        """발급 실패 기록 + 지수 백오프(5초 → 최대 5분) 설정."""
        self._issue_failures += 1
        shift = min(self._issue_failures - 1, 10)  # 2**shift 폭주 방지
        delay = min(TOKEN_RETRY_BACKOFF_BASE * (2**shift), TOKEN_RETRY_BACKOFF_MAX)
        self._retry_after = datetime.now() + delay
        self._last_issue_error = exc
        log.warning(
            "kiwoom.token_issue_failed",
            consecutive=self._issue_failures,
            retry_after_sec=int(delay.total_seconds()),
            error=str(exc)[:200],
        )

    async def invalidate(self) -> None:
        async with self._lock:
            self._token = None
            self._expires_at = None
            self._delete_cache()

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
            token_preview=mask_secret(parsed.token, with_length=True),
            expires_at=self._expires_at.isoformat() if self._expires_at else None,
            mode=self._settings.kiwoom_mode.value,
        )
        self._save_cache()
        return self._token

    # ---- 디스크 영속화 (29단계 PR12) -----------------------------------------

    def _load_cache(self) -> None:
        """시동 시 디스크에서 로드. 만료 임박/손상 시 무시 — 정상 발급으로 fallback."""
        if self._cache_path is None or not self._cache_path.exists():
            return
        try:
            raw = self._cache_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            token = str(data.get("token") or "")
            expires_iso = str(data.get("expires_at") or "")
            if not token or not expires_iso:
                return
            expires_at = datetime.fromisoformat(expires_iso)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            log.warning(
                "kiwoom.token_cache_load_failed",
                path=str(self._cache_path),
                error=str(exc),
            )
            return
        # 만료 마진 안에 있으면 안 쓰고 새로 발급 — 잘못된 캐시로 401 맞는 위험 차단.
        if datetime.now() >= expires_at - REFRESH_MARGIN:
            log.info("kiwoom.token_cache_expired", path=str(self._cache_path))
            return
        self._token = token
        self._expires_at = expires_at
        log.info(
            "kiwoom.token_cache_loaded",
            path=str(self._cache_path),
            token_preview=mask_secret(token, with_length=True),
            expires_at=expires_iso,
        )

    def _save_cache(self) -> None:
        """발급 직후 디스크 저장. POSIX 권한 0o600 (소유자만 R/W)."""
        if self._cache_path is None or self._token is None or self._expires_at is None:
            return
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {"token": self._token, "expires_at": self._expires_at.isoformat()},
                ensure_ascii=False,
            )
            # write → chmod 순서: write 시 umask 영향 받지만 직후 chmod 로 강제.
            self._cache_path.write_text(payload, encoding="utf-8")
            with contextlib.suppress(OSError, NotImplementedError):
                os.chmod(self._cache_path, 0o600)
        except OSError as exc:
            log.error(
                "kiwoom.token_cache_save_failed",
                path=str(self._cache_path),
                error=str(exc),
            )

    def _delete_cache(self) -> None:
        """invalidate 시 디스크 캐시도 제거. 이미 없으면 noop."""
        if self._cache_path is None:
            return
        with contextlib.suppress(FileNotFoundError):
            self._cache_path.unlink()
