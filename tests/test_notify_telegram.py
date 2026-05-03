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


async def test_daytrade_discovery_done_is_korean_not_raw_kwargs(
    notifier: TelegramNotifier,
) -> None:
    """5/3 회귀 — day-trade 발굴 알림이 raw `chosen=[] blocked=[...]` 형태로 가던 문제."""
    async with respx.mock(base_url=TELEGRAM_API_BASE) as mock:
        route = mock.post("/bot12345:SECRETTOKEN/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True}),
        )
        await notifier.notify(
            "daytrade.discovery_done",
            chosen=["005930", "247540"],
            blocked=[("006340", "KOSPI_below_sma")],
            note="매수 후보 2/3",
        )

    body = json.loads(route.calls[0].request.read())
    text = body["text"]
    # 한국어 친절 포맷
    assert "발굴 완료" in text
    assert "005930" in text
    assert "247540" in text
    # raw 형식이 노출되면 안 됨
    assert "chosen=" not in text
    assert "blocked=[" not in text


async def test_daytrade_discovery_done_no_chosen_still_readable(
    notifier: TelegramNotifier,
) -> None:
    """게이트 차단으로 chosen=[] 인 케이스도 사람이 읽을 수 있게."""
    async with respx.mock(base_url=TELEGRAM_API_BASE) as mock:
        route = mock.post("/bot12345:SECRETTOKEN/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True}),
        )
        await notifier.notify(
            "daytrade.discovery_done",
            chosen=[],
            blocked=[
                ("006340", "KOSPI_below_sma"),
                ("900300", "KOSDAQ_below_sma"),
                ("025860", "KOSPI_below_sma"),
            ],
            note="후보 3건 모두 지수 게이트 차단 — 매수 없이 청산 시각까지 대기.",
        )

    text = json.loads(route.calls[0].request.read())["text"]
    assert "발굴 완료" in text
    assert "0건" in text
    assert "차단" in text
    assert "006340" in text  # 차단 종목도 보임
    assert "chosen=" not in text


async def test_runner_liquidate_done_korean_format(notifier: TelegramNotifier) -> None:
    async with respx.mock(base_url=TELEGRAM_API_BASE) as mock:
        route = mock.post("/bot12345:SECRETTOKEN/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True}),
        )
        await notifier.notify(
            "runner.liquidate_done", reason="liquidate_at_reached", count=2, dry_run=True
        )
    text = json.loads(route.calls[0].request.read())["text"]
    assert "청산 완료" in text
    assert "2종목" in text
    assert "dry-run" in text
