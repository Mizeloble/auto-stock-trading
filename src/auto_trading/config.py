"""환경 설정 로더.

`.env` 파일에서 키움 AppKey/Secret 과 동작 모드를 읽어, 실수로 실거래 서버에
붙는 일이 없도록 한 곳에서 관리한다. 초보자 안전장치로 기본값은 `mock`.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class KiwoomMode(StrEnum):
    MOCK = "mock"
    REAL = "real"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    kiwoom_mode: KiwoomMode = KiwoomMode.MOCK
    kiwoom_app_key: SecretStr = SecretStr("")
    kiwoom_app_secret: SecretStr = SecretStr("")
    kiwoom_account_no: str = ""

    log_level: str = Field(default="INFO")

    @property
    def rest_base_url(self) -> str:
        return (
            "https://mockapi.kiwoom.com"
            if self.kiwoom_mode is KiwoomMode.MOCK
            else "https://api.kiwoom.com"
        )

    @property
    def ws_url(self) -> str:
        host = "mockapi.kiwoom.com" if self.kiwoom_mode is KiwoomMode.MOCK else "api.kiwoom.com"
        return f"wss://{host}:10000/api/dostk/websocket"

    @property
    def is_mock(self) -> bool:
        return self.kiwoom_mode is KiwoomMode.MOCK


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
