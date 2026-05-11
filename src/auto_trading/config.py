"""환경 설정 로더.

`.env` 파일에서 키움 AppKey/Secret 과 동작 모드를 읽어, 실수로 실거래 서버에
붙는 일이 없도록 한 곳에서 관리한다. 초보자 안전장치로 기본값은 `mock`.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

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
        # 빈 문자열로 export 된 환경변수는 무시하고 .env 값을 우선 사용한다.
        # 예: 일부 호스트 환경에서 ANTHROPIC_API_KEY="" 가 미리 export 되어 있어
        # .env 의 실제 키를 덮어쓰던 문제를 방지.
        env_ignore_empty=True,
    )

    kiwoom_mode: KiwoomMode = KiwoomMode.MOCK
    kiwoom_app_key: SecretStr = SecretStr("")
    kiwoom_app_secret: SecretStr = SecretStr("")
    kiwoom_account_no: str = ""
    # 29단계 PR12 (#21) — OAuth 토큰 디스크 캐시 경로. None 이면 메모리 only.
    # 운영에선 .env 의 `KIWOOM_TOKEN_CACHE_PATH=data/auth/token.json` 으로 활성.
    # 시크릿이라 파일 권한 0o600 (POSIX). data/ 는 .gitignore 자동 제외.
    kiwoom_token_cache_path: Path | None = None

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

    # 29단계 PR15 (#16) — LLM 비용 누적 추적. None 이면 비활성 (기존 동작 호환).
    # 운영에선 .env 의 `LLM_COST_PATH=data/llm-cost.jsonl` 으로 활성.
    # 한도 초과 시 LLMBudgetExceeded raise → 호출자가 catch 해서 사용자에게 안내.
    llm_cost_path: Path | None = None
    llm_monthly_budget_usd: float = 5.0  # memory cost discipline — 월 $5

    # 29단계 PR19 (#20) — 신호→주문 latency 모니터링. None 이면 비활성.
    # 운영에선 .env 의 `LATENCY_LOG_PATH=data/latency.jsonl` 으로 활성.
    # 누적 후 `auto-trade latency` 로 일별 p50/p95/p99 통계 확인.
    latency_log_path: Path | None = None

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
