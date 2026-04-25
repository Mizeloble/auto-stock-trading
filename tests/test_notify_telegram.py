"""TelegramNotifier 단위 테스트.

목적:
- sendMessage 정상 호출 시 path/body 가 정확한지
- API 가 4xx/5xx 응답해도 예외 swallow + log.warning 만 (트레이딩 막지 않음)
- 토큰이 어떤 출력에도 노출되지 않는지 (마스킹 회귀)
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from auto_trading.notify.telegram import TELEGRAM_API_BASE, TelegramNotifier


@pytest.fixture
def notifier() -> TelegramNotifier:
    return TelegramNotifier(token="12345:SECRETTOKEN", chat_id="42")


async def test_sendmessage_success_posts_correct_path_and_body(
    notifier: TelegramNotifier,
) -> None:
    async with respx.mock(base_url=TELEGRAM_API_BASE) as mock:
        route = mock.post("/bot12345:SECRETTOKEN/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True}),
        )

        await notifier.notify(
            "runner.order_accepted",
            order_no="0000123",
            symbol="005930",
            side="buy",
            qty=10,
        )

    assert route.called
    body = json.loads(route.calls[0].request.read())
    assert body["chat_id"] == "42"
    assert "체결 접수" in body["text"]
    assert "005930" in body["text"]
    assert "BUY" in body["text"]
    assert "0000123" in body["text"]


async def test_send_failure_is_swallowed_and_logged(
    notifier: TelegramNotifier, capsys: pytest.CaptureFixture[str]
) -> None:
    """API 가 500 에러를 던져도 예외가 트레이딩 코드까지 전파돼선 안 된다."""
    async with respx.mock(base_url=TELEGRAM_API_BASE) as mock:
        mock.post("/bot12345:SECRETTOKEN/sendMessage").mock(
            return_value=httpx.Response(500, text="internal error"),
        )

        # 어떤 예외도 raise 하지 않아야 함
        await notifier.notify("runner.order_failed", error="boom", symbol="005930")

    # structlog 는 stdout 으로 출력 — "notify.send_failed" 이벤트가 한번 이상 찍혀야 한다
    captured = capsys.readouterr()
    assert "notify.send_failed" in captured.out
    assert "status=500" in captured.out


async def test_token_is_never_exposed_in_repr(notifier: TelegramNotifier) -> None:
    r = repr(notifier)
    assert "SECRETTOKEN" not in r
    assert "1234" in r  # 앞 4자만 노출
    assert "…" in r
