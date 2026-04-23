"""키움 REST API 요청/응답 pydantic 모델."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class KiwoomBase(BaseModel):
    """공통 베이스 — 추가 필드(문서에 없는 필드)는 무시하고 로그만."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


# --- 인증 ---


class TokenResponse(KiwoomBase):
    """POST /oauth2/token 응답.

    키움 문서 표기가 케이스 바이 케이스라, 대표 alias 두어 매핑한다.
    """

    token: str = Field(validation_alias="token")
    token_type: str = Field(default="Bearer", validation_alias="token_type")
    expires_dt: str | None = Field(default=None, validation_alias="expires_dt")

    @property
    def expires_at(self) -> datetime | None:
        """`expires_dt` 가 `yyyyMMddHHmmss` 포맷이면 파싱, 아니면 None."""
        if not self.expires_dt:
            return None
        raw = self.expires_dt.strip()
        for fmt in ("%Y%m%d%H%M%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue
        return None


class KiwoomError(Exception):
    """키움 응답의 비-정상(return_code != 0) 래핑."""

    def __init__(self, code: str, message: str, payload: dict | None = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.payload = payload or {}
