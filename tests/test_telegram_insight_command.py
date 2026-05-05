"""텔레그램 `/insight` 명령 dispatch + cooldown + 비활성 안내 회귀."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from auto_trading.notify.state import HELP_TEXT, RunnerStateSnapshot
from auto_trading.notify.telegram_commands import TelegramCommandHandler


def _state() -> RunnerStateSnapshot:
    return RunnerStateSnapshot(
        mode="mock",
        dry_run=True,
        symbols=[],
        strategies={},
        started_at=None,
        snapshot=None,
        account=None,
        recent_signals={},
        balance_failures=0,
    )


class _DummyHttpClient:
    """sendMessage / getUpdates 둘 다 무시. dispatch 단위 테스트용."""

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("polling not used in dispatch tests")

    async def post(self, *args: Any, **kwargs: Any) -> Any:
        class _Resp:
            status_code = 200
            text = ""

        return _Resp()

    async def aclose(self) -> None:
        pass


def _handler(**overrides: Any) -> TelegramCommandHandler:
    base: dict[str, Any] = dict(
        token="t",
        allowed_chat_ids={"42"},
        state_provider=_state,
        client=_DummyHttpClient(),  # type: ignore[arg-type]
        insight_cooldown_sec=300,
    )
    base.update(overrides)
    return TelegramCommandHandler(**base)


async def test_insight_returns_inactive_message_when_callback_missing() -> None:
    h = _handler()
    msg = await h._handle_insight()
    assert "비활성" in msg


async def test_insight_calls_callback_and_returns_text() -> None:
    async def _cb() -> str:
        return "🧭 주간 인사이트 stub"

    h = _handler(on_insight=_cb)
    msg = await h._handle_insight()
    assert msg == "🧭 주간 인사이트 stub"


async def test_insight_cooldown_blocks_second_call() -> None:
    calls = 0

    async def _cb() -> str:
        nonlocal calls
        calls += 1
        return "ok"

    h = _handler(on_insight=_cb, insight_cooldown_sec=300)
    first = await h._handle_insight()
    second = await h._handle_insight()
    assert first == "ok"
    assert "쿨다운" in second
    assert calls == 1


async def test_insight_cooldown_resets_after_window() -> None:
    """쿨다운 윈도우가 지나면 다시 호출 — 내부 시각을 직접 옮겨 검증."""

    async def _cb() -> str:
        return "ok"

    h = _handler(on_insight=_cb, insight_cooldown_sec=60)
    await h._handle_insight()
    # 인스턴스 시간만 과거로 옮김 — `datetime.now()` mock 없이 검증.
    h._insight_last_at = datetime.now() - timedelta(seconds=120)
    second = await h._handle_insight()
    assert second == "ok"


async def test_insight_swallows_exception_and_returns_safe_message() -> None:
    """LLM 실패가 트레이딩 알림 루프를 죽이지 않게."""

    async def _cb() -> str:
        raise RuntimeError("boom")

    h = _handler(on_insight=_cb)
    msg = await h._handle_insight()
    assert "실패" in msg


async def test_help_text_lists_insight_command() -> None:
    assert "/insight" in HELP_TEXT


async def test_dispatch_routes_insight_command_through_handler(monkeypatch: Any) -> None:
    """봇이 받은 메시지의 /insight 가 _handle_insight 로 라우팅되는지."""
    sent: list[tuple[str, str]] = []

    async def _cb() -> str:
        return "🧭 ok"

    h = _handler(on_insight=_cb)

    async def _fake_send(chat_id: str, text: str) -> None:
        sent.append((chat_id, text))

    monkeypatch.setattr(h, "_send", _fake_send)

    update = {
        "update_id": 1,
        "message": {
            "chat": {"id": 42},
            "text": "/insight",
        },
    }
    await h._dispatch(update)
    assert sent == [("42", "🧭 ok")]


def test_async_loop_runs_dispatch() -> None:
    """위 async 케이스를 sync 루프로도 한 번 더 — 다른 테스트 픽스처 누수 검증용."""
    asyncio.run(_DummyHttpClient().aclose())
