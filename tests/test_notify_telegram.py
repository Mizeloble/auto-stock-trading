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


async def test_runner_stopped_day_trade_self_termination_includes_hint(
    notifier: TelegramNotifier,
) -> None:
    """5/3 사용자 피드백 회귀 — day-trade 자기 종료 후 /status 무응답으로 헷갈리던 케이스.
    stopped_before_universe / no_candidates_stopping 류 reason 일 때 다음 시동 안내 한 줄.
    """
    async with respx.mock(base_url=TELEGRAM_API_BASE) as mock:
        route = mock.post("/bot12345:SECRETTOKEN/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True}),
        )
        await notifier.notify("runner.stopped", reason="stopped_before_universe")

    text = json.loads(route.calls[0].request.read())["text"]
    assert "paper 정지" in text
    assert "다음 시동" in text
    assert "09:00" in text


async def test_runner_stopped_normal_reason_no_hint(notifier: TelegramNotifier) -> None:
    """일반 stop (외부 SIGTERM, 사용자 /stop) 은 안내 없이 깔끔하게."""
    async with respx.mock(base_url=TELEGRAM_API_BASE) as mock:
        route = mock.post("/bot12345:SECRETTOKEN/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True}),
        )
        await notifier.notify("runner.stopped", reason="정상 종료")

    text = json.loads(route.calls[0].request.read())["text"]
    assert "paper 정지" in text
    assert "다음 시동" not in text  # 안내 없음 — 사용자가 직접 끈 거니 헷갈림 X


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


async def test_runner_kill_switch_korean_format(notifier: TelegramNotifier) -> None:
    """23단계 Phase B — 일일 손실 한도 강제 청산 알림."""
    async with respx.mock(base_url=TELEGRAM_API_BASE) as mock:
        route = mock.post("/bot12345:SECRETTOKEN/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True}),
        )
        await notifier.notify("runner.kill_switch_triggered", daily_loss_limit=0.03)
    text = json.loads(route.calls[0].request.read())["text"]
    assert "일일 손실 한도 도달" in text
    assert "-3.0%" in text
    assert "강제 청산" in text


async def test_position_guard_fired_korean_format(notifier: TelegramNotifier) -> None:
    """23단계 Phase C — guard 발화 시 stop/trailing/partial 한국어 매핑."""
    async with respx.mock(base_url=TELEGRAM_API_BASE) as mock:
        route = mock.post("/bot12345:SECRETTOKEN/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True}),
        )
        await notifier.notify(
            "position_guard.fired",
            symbol="018880",
            reason="stop_loss",
            qty=10,
            price=4950.0,
            dry_run=True,
        )
    text = json.loads(route.calls[0].request.read())["text"]
    assert "🛟 손절" in text
    assert "018880" in text
    assert "10주" in text
    assert "4,950원" in text
    assert "(시뮬)" in text


async def test_position_guard_partial_take_korean_format(notifier: TelegramNotifier) -> None:
    """부분 익절 발화는 별도 이모지 + 라벨."""
    async with respx.mock(base_url=TELEGRAM_API_BASE) as mock:
        route = mock.post("/bot12345:SECRETTOKEN/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True}),
        )
        await notifier.notify(
            "position_guard.fired",
            symbol="010170",
            reason="partial_take",
            qty=5,
            price=10200.0,
            dry_run=False,
        )
    text = json.loads(route.calls[0].request.read())["text"]
    assert "💵 부분 익절" in text
    assert "010170" in text
    assert "(시뮬)" not in text
