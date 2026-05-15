"""tests/ 공용 픽스처."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_token_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """테스트가 운영 토큰 캐시(.env 의 data/auth/token.json)를 덮어쓰지 못하게.

    Settings 가 테스트에서도 .env 를 읽어 kiwoom_token_cache_path 가 실제 경로로
    잡혔고, 가짜 'tkn' 토큰을 발급하는 단위 테스트가 _save_cache 로 운영 캐시를
    오염시켜 다음 시동을 통째로 날린 사고(2026-05-15)가 있었다. env_ignore_empty
    =True 라 빈 문자열은 무시되므로 반드시 실재하는 임시 경로로 덮는다.
    """

    monkeypatch.setenv("KIWOOM_TOKEN_CACHE_PATH", str(tmp_path / "token.json"))


@pytest.fixture
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """CLI 가 상대 경로로 logs/data 를 만들지 않게 cwd 를 임시 폴더로.

    KIWOOM/TELEGRAM/ANTHROPIC 환경변수도 빈 값으로 덮어 .env 의존 차단 +
    실제 외부 호출 회로를 끊는다 (mock/fake 가 인계받음).
    """

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KIWOOM_APP_KEY", "k")
    monkeypatch.setenv("KIWOOM_APP_SECRET", "s")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    return tmp_path
