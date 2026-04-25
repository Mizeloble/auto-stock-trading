"""통합 테스트 공통 픽스처.

- 모의투자 키(`KIWOOM_APP_KEY`, `KIWOOM_APP_SECRET`)가 세팅돼 있을 때만 실행된다.
- 키가 없으면 모든 통합 테스트가 skip 된다.
- `KIWOOM_MODE` 는 기본 `mock` 으로 고정 — 실거래 계정으로 통합 테스트 금지.
- 개별 테스트 파일은 `pytestmark = pytest.mark.integration` 을 선언해야 한다.
"""

from __future__ import annotations

import os

import pytest


def _has_mock_keys() -> bool:
    return bool(os.environ.get("KIWOOM_APP_KEY") and os.environ.get("KIWOOM_APP_SECRET"))


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if _has_mock_keys():
        return
    skip_marker = pytest.mark.skip(reason="모의투자 키(.env) 미설정 — 통합 테스트 skip")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_marker)


@pytest.fixture(autouse=True)
def _force_mock_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """통합 테스트는 실거래 모드에서 절대 돌지 않도록 강제."""
    monkeypatch.setenv("KIWOOM_MODE", "mock")
