"""DiscordNotifier + MultiNotifier 회귀.

29단계 PR25 (#12 1차).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from auto_trading.notify.discord import DiscordNotifier
from auto_trading.notify.multi import MultiNotifier

WEBHOOK = "https://discord.com/api/webhooks/123/abcdef"


# ---- DiscordNotifier --------------------------------------------------------


def test_discord_notifier_requires_webhook_url() -> None:
    with pytest.raises(ValueError, match="webhook_url"):
        DiscordNotifier(webhook_url="")


def test_discord_notifier_repr_masks_webhook() -> None:
    n = DiscordNotifier(webhook_url=WEBHOOK)
    repr_str = repr(n)
    # URL 의 token 부분 (token 시작 4자만 노출).
    assert "abcdef" not in repr_str  # full token 노출 안 됨
    assert "DiscordNotifier" in repr_str


async def test_discord_notifier_posts_content() -> None:
    async with respx.mock() as mock:
        route = mock.post(WEBHOOK).mock(return_value=httpx.Response(204))
        notifier = DiscordNotifier(webhook_url=WEBHOOK)
        try:
            await notifier.notify("runner.started", strategy="sma", symbols="005930", dry_run=True)
        finally:
            await notifier.aclose()

    assert route.called
    body = route.calls[0].request.read().decode()
    # content 필드에 메시지 들어감.
    assert "content" in body
    assert "paper" in body  # _format_message 결과


async def test_discord_notifier_swallows_http_error() -> None:
    """4xx/5xx 응답이 와도 예외 X — 트레이딩 핫패스 보호."""
    async with respx.mock() as mock:
        mock.post(WEBHOOK).mock(return_value=httpx.Response(400, text="bad"))
        notifier = DiscordNotifier(webhook_url=WEBHOOK)
        try:
            # 예외 X.
            await notifier.notify("runner.started", strategy="sma", symbols="X", dry_run=False)
        finally:
            await notifier.aclose()


async def test_discord_notifier_swallows_network_error() -> None:
    """네트워크 끊김 / 타임아웃 → swallow."""
    async with respx.mock() as mock:
        mock.post(WEBHOOK).mock(side_effect=httpx.ConnectError("network down"))
        notifier = DiscordNotifier(webhook_url=WEBHOOK)
        try:
            await notifier.notify("runner.started", strategy="x", symbols="x", dry_run=False)
        finally:
            await notifier.aclose()


# ---- MultiNotifier ----------------------------------------------------------


def test_multi_notifier_requires_at_least_one() -> None:
    with pytest.raises(ValueError, match="최소 1개"):
        MultiNotifier([])


async def test_multi_notifier_fans_out_to_all() -> None:
    """모든 notifier 에 동일 이벤트 전달."""
    a = AsyncMock()
    b = AsyncMock()
    multi = MultiNotifier([a, b])
    await multi.notify("runner.started", strategy="sma", symbols="X", dry_run=True)
    a.notify.assert_awaited_once_with("runner.started", strategy="sma", symbols="X", dry_run=True)
    b.notify.assert_awaited_once_with("runner.started", strategy="sma", symbols="X", dry_run=True)


async def test_multi_notifier_continues_after_one_fails() -> None:
    """한 notifier 가 예외 raise 해도 다른 notifier 는 그대로 호출."""
    a = AsyncMock(notify=AsyncMock(side_effect=RuntimeError("boom")))
    b = AsyncMock()
    multi = MultiNotifier([a, b])
    await multi.notify("test", value=1)
    a.notify.assert_awaited_once()
    b.notify.assert_awaited_once()


async def test_multi_notifier_aclose_calls_all() -> None:
    a = AsyncMock()
    b = AsyncMock()
    multi = MultiNotifier([a, b])
    await multi.aclose()
    a.aclose.assert_awaited_once()
    b.aclose.assert_awaited_once()


def test_multi_notifier_repr_lists_backend_names() -> None:
    a = AsyncMock()
    a.__class__.__name__ = "FakeA"
    b = AsyncMock()
    b.__class__.__name__ = "FakeB"
    multi = MultiNotifier([a, b])
    r = repr(multi)
    assert "MultiNotifier" in r
