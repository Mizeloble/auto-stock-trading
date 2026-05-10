"""tests/ 공용 픽스처."""

from __future__ import annotations

from pathlib import Path

import pytest


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
