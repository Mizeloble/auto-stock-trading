"""TelegramCommandHandler 단위 테스트.

검증:
- 명령별 응답 포맷 (`format_*` 헬퍼 결합)
- 화이트리스트 미통과 시 sendMessage 호출 0회
- getUpdates offset 전진 (배치 두 번 받았을 때)
- sendMessage 5xx swallow
- 알 수 없는 명령은 /help 와 동일 응답
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

import httpx
import pytest
import respx

from auto_trading.account.balance import AccountSnapshot, Holding
from auto_trading.notify.state import (
    HELP_TEXT,
    RunnerStateSnapshot,
    format_balance,
    format_last_signal,
    format_pnl,
    format_positions,
    format_status,
)
from auto_trading.notify.telegram import TELEGRAM_API_BASE
from auto_trading.notify.telegram_commands import TelegramCommandHandler
from auto_trading.risk.manager import AccountState
from auto_trading.strategy.base import Side, Signal

TOKEN = "12345:CMDTOKEN"
ALLOWED = "777"
DENIED = "999"


# ---- 픽스처 ---------------------------------------------------------------


def _empty_state(**overrides: object) -> RunnerStateSnapshot:
    base = dict(
        mode="mock",
        dry_run=False,
        symbols=["005930"],
        strategies={"005930": "SmaCrossover"},
        started_at=datetime(2026, 4, 25, 9, 0),
        snapshot=None,
        account=None,
        recent_signals={},
        balance_failures=0,
    )
    base.update(overrides)
    return RunnerStateSnapshot(**base)  # type: ignore[arg-type]


def _full_snapshot() -> AccountSnapshot:
    return AccountSnapshot(
        deposit=Decimal(50_000_000),
        total_evaluation=Decimal(51_500_000),
        total_pnl=Decimal(150_000),
        holdings=[
            Holding(
                symbol="005930",
                name="삼성전자",
                quantity=10,
                avg_price=Decimal("60000"),
                current_price=Decimal("65000"),
                market_value=Decimal("650000"),
                pnl=Decimal("50000"),
                pnl_pct=Decimal("8.33"),
            ),
            Holding(
                symbol="000660",
                name="SK하이닉스",
                quantity=5,
                avg_price=Decimal("120000"),
                current_price=Decimal("130000"),
                market_value=Decimal("650000"),
                pnl=Decimal("50000"),
                pnl_pct=Decimal("8.33"),
            ),
        ],
    )


def _account_state() -> AccountState:
    return AccountState(
        deposit=50_000_000,
        total_equity=51_500_000,
        positions={"005930": 10, "000660": 5},
        avg_prices={"005930": 60_000.0, "000660": 120_000.0},
        day_start_equity=50_000_000,
    )


def _handler(state: RunnerStateSnapshot, client: httpx.AsyncClient) -> TelegramCommandHandler:
    return TelegramCommandHandler(
        token=TOKEN,
        allowed_chat_ids={ALLOWED},
        state_provider=lambda: state,
        client=client,
        poll_timeout=1,
    )


# ---- format_* 함수 직접 검증 (렌더링 단위) ---------------------------------


def test_format_status_includes_mode_symbols_strategies() -> None:
    state = _empty_state()
    text = format_status(state, now=datetime(2026, 4, 25, 9, 30))
    assert "mock" in text
    assert "005930" in text
    assert "SmaCrossover" in text
    assert "30분" in text  # uptime


def test_format_balance_no_snapshot() -> None:
    assert "잔고 조회 전" in format_balance(_empty_state())


def test_format_balance_with_holdings_shows_deposit_and_per_symbol() -> None:
    text = format_balance(_empty_state(snapshot=_full_snapshot()))
    assert "50,000,000원" in text  # 예수금
    assert "51,500,000원" in text  # 총평가
    assert "+150,000원" in text  # 평가손익 부호
    assert "삼성전자" in text
    assert "005930" in text
    assert "SK하이닉스" in text
    assert "000660" in text


def test_format_positions_empty() -> None:
    assert format_positions(_empty_state()) == "포지션 없음"


def test_format_positions_with_holdings() -> None:
    text = format_positions(_empty_state(snapshot=_full_snapshot()))
    assert "005930" in text
    assert "10주" in text
    assert "60,000원" in text
    assert "000660" in text
    assert "5주" in text


def test_format_pnl_with_account_and_snapshot() -> None:
    state = _empty_state(
        snapshot=_full_snapshot(),
        account=_account_state(),
    )
    text = format_pnl(state)
    # 당일: 51_500_000 - 50_000_000 = +1,500,000원 (+3.00%)
    assert "+1,500,000원" in text
    assert "+3.00%" in text
    # 누적: total_pnl = 150,000
    assert "+150,000원" in text


def test_format_pnl_no_data() -> None:
    text = format_pnl(_empty_state())
    assert "당일: -" in text
    assert "누적 평가손익: -" in text


def test_format_last_signal_empty() -> None:
    assert format_last_signal(_empty_state()) == "아직 시그널 없음"


def test_format_last_signal_with_signals() -> None:
    state = _empty_state(
        recent_signals={
            "005930": Signal(
                symbol="005930",
                side=Side.BUY,
                weight=0.5,
                reason="golden cross",
                ts=datetime(2026, 4, 25, 10, 30),
                price_hint=Decimal("65000"),
            ),
        },
    )
    text = format_last_signal(state)
    assert "005930" in text
    assert "BUY" in text
    assert "golden cross" in text
    assert "65,000원" in text
    assert "10:30" in text


# ---- 핸들러 dispatch 흐름 (respx 로 HTTP mock) -----------------------------


async def test_dispatch_unauthorized_chat_no_sendmessage_call() -> None:
    """화이트리스트에 없는 chat 은 무응답 (sendMessage 호출 0회)."""
    async with (
        httpx.AsyncClient(base_url=TELEGRAM_API_BASE) as client,
        respx.mock(base_url=TELEGRAM_API_BASE, assert_all_called=False) as mock,
    ):
        send_route = mock.post(f"/bot{TOKEN}/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True}),
        )
        handler = _handler(_empty_state(), client)
        update = {
            "update_id": 1,
            "message": {
                "chat": {"id": int(DENIED)},
                "text": "/status",
            },
        }
        await handler._dispatch(update)

        assert send_route.call_count == 0


async def test_dispatch_authorized_status_sends_response() -> None:
    """화이트리스트 chat 의 /status → sendMessage body 에 mode/symbols 포함."""
    async with (
        httpx.AsyncClient(base_url=TELEGRAM_API_BASE) as client,
        respx.mock(base_url=TELEGRAM_API_BASE) as mock,
    ):
        send_route = mock.post(f"/bot{TOKEN}/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True}),
        )
        handler = _handler(_empty_state(), client)
        update = {
            "update_id": 1,
            "message": {
                "chat": {"id": int(ALLOWED)},
                "text": "/status",
            },
        }
        await handler._dispatch(update)

        assert send_route.called
        body = json.loads(send_route.calls[0].request.read())
        assert body["chat_id"] == ALLOWED
        assert "mock" in body["text"]
        assert "005930" in body["text"]


async def test_dispatch_balance_command_uses_state_provider() -> None:
    state = _empty_state(snapshot=_full_snapshot())
    async with (
        httpx.AsyncClient(base_url=TELEGRAM_API_BASE) as client,
        respx.mock(base_url=TELEGRAM_API_BASE) as mock,
    ):
        send_route = mock.post(f"/bot{TOKEN}/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True}),
        )
        handler = _handler(state, client)
        update = {
            "update_id": 1,
            "message": {
                "chat": {"id": int(ALLOWED)},
                "text": "/balance",
            },
        }
        await handler._dispatch(update)

        body = json.loads(send_route.calls[0].request.read())
        assert "삼성전자" in body["text"]
        assert "50,000,000원" in body["text"]


async def test_dispatch_unknown_command_returns_help() -> None:
    async with (
        httpx.AsyncClient(base_url=TELEGRAM_API_BASE) as client,
        respx.mock(base_url=TELEGRAM_API_BASE) as mock,
    ):
        send_route = mock.post(f"/bot{TOKEN}/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True}),
        )
        handler = _handler(_empty_state(), client)
        update = {
            "update_id": 1,
            "message": {
                "chat": {"id": int(ALLOWED)},
                "text": "/foobar",
            },
        }
        await handler._dispatch(update)

        body = json.loads(send_route.calls[0].request.read())
        assert body["text"] == HELP_TEXT


async def test_dispatch_send_failure_is_swallowed() -> None:
    """sendMessage 가 500 을 던져도 dispatch 자체는 예외 없이 끝난다."""
    async with (
        httpx.AsyncClient(base_url=TELEGRAM_API_BASE) as client,
        respx.mock(base_url=TELEGRAM_API_BASE) as mock,
    ):
        mock.post(f"/bot{TOKEN}/sendMessage").mock(
            return_value=httpx.Response(500, text="internal error"),
        )
        handler = _handler(_empty_state(), client)
        update = {
            "update_id": 1,
            "message": {"chat": {"id": int(ALLOWED)}, "text": "/status"},
        }
        # 예외가 raise 되면 테스트 실패
        await handler._dispatch(update)


# ---- 폴링 offset ---------------------------------------------------------


async def test_poll_advances_offset_across_batches() -> None:
    async with (
        httpx.AsyncClient(base_url=TELEGRAM_API_BASE) as client,
        respx.mock(base_url=TELEGRAM_API_BASE) as mock,
    ):
        mock.get(f"/bot{TOKEN}/getUpdates").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": [
                            {"update_id": 10, "message": {}},
                            {"update_id": 12, "message": {}},
                        ],
                    },
                ),
                httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": [{"update_id": 15, "message": {}}],
                    },
                ),
                httpx.Response(200, json={"ok": True, "result": []}),
            ],
        )
        handler = _handler(_empty_state(), client)

        updates1 = await handler._poll()
        assert len(updates1) == 2
        assert handler._offset == 13  # 12 + 1

        updates2 = await handler._poll()
        assert len(updates2) == 1
        assert handler._offset == 16  # 15 + 1

        updates3 = await handler._poll()
        assert updates3 == []
        assert handler._offset == 16  # 변동 없음


async def test_poll_handles_http_error_gracefully() -> None:
    """getUpdates 가 5xx 를 던지면 빈 리스트만 리턴, offset 도 변동 없음."""
    async with (
        httpx.AsyncClient(base_url=TELEGRAM_API_BASE) as client,
        respx.mock(base_url=TELEGRAM_API_BASE) as mock,
    ):
        mock.get(f"/bot{TOKEN}/getUpdates").mock(
            return_value=httpx.Response(502, text="bad gateway"),
        )
        handler = _handler(_empty_state(), client)
        updates = await handler._poll()
        assert updates == []
        assert handler._offset == 0


# ---- 인스턴스화 가드 ------------------------------------------------------


def test_handler_requires_token() -> None:
    with pytest.raises(ValueError, match="token"):
        TelegramCommandHandler(token="", allowed_chat_ids={"1"}, state_provider=_empty_state)


def test_handler_requires_allowed_chat_ids() -> None:
    with pytest.raises(ValueError, match="allowed_chat_ids"):
        TelegramCommandHandler(token=TOKEN, allowed_chat_ids=set(), state_provider=_empty_state)


def test_handler_repr_does_not_leak_token() -> None:
    handler = TelegramCommandHandler(
        token=TOKEN, allowed_chat_ids={"1"}, state_provider=_empty_state
    )
    r = repr(handler)
    assert "CMDTOKEN" not in r
    assert "1234" in r
    assert "…" in r
