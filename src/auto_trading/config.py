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

    # 텔레그램 알림 (선택). 두 값 모두 채워졌을 때만 활성화 — 미설정 시 NoopNotifier 폴백.
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""
    # 텔레그램 인커밍 명령(/status 등) 허용 chat_id 화이트리스트 (콤마 구분).
    # 비어있으면 인커밍 명령 비활성 — 봇은 어떤 메시지에도 응답하지 않는다.
    telegram_allowed_chat_ids: str = ""

    # Claude API (선택). 키가 있을 때만 `auto-trade strategy advise` 가 동작.
    # 모델 ID 는 환경별 override 가능 — Sonnet 계열로 비용 낮출 수 있음.
    anthropic_api_key: SecretStr = SecretStr("")
    anthropic_model: str = "claude-opus-4-7"

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

    @property
    def has_telegram(self) -> bool:
        return bool(self.telegram_bot_token.get_secret_value() and self.telegram_chat_id)

    @property
    def parsed_allowed_chat_ids(self) -> set[str]:
        return {p.strip() for p in self.telegram_allowed_chat_ids.split(",") if p.strip()}

    @property
    def has_telegram_commands(self) -> bool:
        return self.has_telegram and bool(self.parsed_allowed_chat_ids)

    @property
    def has_anthropic(self) -> bool:
        return bool(self.anthropic_api_key.get_secret_value())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
