"""TelegramCommandHandler 단위 테스트.

검증:
- 명령별 응답 포맷 (`format_*` 헬퍼 결합)
- 화이트리스트 미통과 시 sendMessage 호출 0회
- getUpdates offset 전진 (배치 두 번 받았을 때)
- sendMessage 5xx swallow
- 알 수 없는 명령은 /help 와 동일 응답
- run() 루프의 transport 예외(타임아웃 등) 는 debug, 그 외만 warning
"""

from __future__ import annotations

import asyncio
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
    # 29단계 PR27 — 평가손익 라인은 holdings.pnl 합산 (snap.total_pnl 무시).
    # _full_snapshot 의 holdings 두 종목 pnl 합은 +100,000.
    assert "평가손익: +100,000원" in text
    assert "삼성전자" in text
    assert "005930" in text
    assert "SK하이닉스" in text
    assert "000660" in text


def test_format_balance_sums_holdings_when_kiwoom_total_pnl_zero() -> None:
    """5/12 회귀 — mock 응답이 tot_evltv_prft 를 안 보내 snap.total_pnl=0 으로 들어와도
    종목별 손익 합산이 평가손익 라인에 그대로 표기되어야 한다.
    """
    snap = AccountSnapshot(
        deposit=Decimal(46_682_430),
        total_evaluation=Decimal(6_371_960),
        total_pnl=Decimal(0),  # mock 응답이 비어서 0 으로 들어옴
        holdings=[
            Holding(
                symbol="005930",
                name="삼성전자",
                quantity=11,
                avg_price=Decimal("279000"),
                current_price=Decimal("278900"),
                market_value=Decimal("3069000"),
                pnl=Decimal("-27617"),
                pnl_pct=Decimal("-0.90"),
            ),
            Holding(
                symbol="356680",
                name="엑스게이트",
                quantity=212,
                avg_price=Decimal("15567"),
                current_price=Decimal("15580"),
                market_value=Decimal("3302960"),
                pnl=Decimal("-27045"),
                pnl_pct=Decimal("-0.82"),
            ),
        ],
    )
    text = format_balance(_empty_state(snapshot=snap))
    assert "평가손익: -54,662원" in text  # -27617 + -27045
    assert "평가손익: 0원" not in text


def test_format_balance_zero_holdings_keeps_zero_total() -> None:
    """보유 없음이면 합계도 0원 — 기존 출력 유지."""
    snap = AccountSnapshot(
        deposit=Decimal(50_000_000),
        total_evaluation=Decimal(0),
        total_pnl=Decimal(0),
        holdings=[],
    )
    text = format_balance(_empty_state(snapshot=snap))
    assert "평가손익: 0원" in text
    assert "보유 종목: 없음" in text


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


# ---- v3 제어 명령 (/pause /resume /stop) ---------------------------------


def _handler_with_control(
    state: RunnerStateSnapshot,
    client: httpx.AsyncClient,
    *,
    on_pause: object = None,
    on_resume: object = None,
    on_stop: object = None,
) -> TelegramCommandHandler:
    return TelegramCommandHandler(
        token=TOKEN,
        allowed_chat_ids={ALLOWED},
        state_provider=lambda: state,
        client=client,
        poll_timeout=1,
        on_pause=on_pause,  # type: ignore[arg-type]
        on_resume=on_resume,  # type: ignore[arg-type]
        on_stop=on_stop,  # type: ignore[arg-type]
    )


async def test_dispatch_pause_invokes_callback_and_acknowledges() -> None:
    """/pause → on_pause() 호출 1회 + chat 응답에 '일시정지' 안내 포함."""
    pause_calls: list[None] = []

    async with (
        httpx.AsyncClient(base_url=TELEGRAM_API_BASE) as client,
        respx.mock(base_url=TELEGRAM_API_BASE) as mock,
    ):
        send_route = mock.post(f"/bot{TOKEN}/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True}),
        )
        handler = _handler_with_control(
            _empty_state(), client, on_pause=lambda: pause_calls.append(None)
        )
        await handler._dispatch(
            {
                "update_id": 1,
                "message": {"chat": {"id": int(ALLOWED)}, "text": "/pause"},
            }
        )

        assert len(pause_calls) == 1
        body = json.loads(send_route.calls[0].request.read())
        assert "일시정지" in body["text"]


async def test_dispatch_resume_invokes_callback_and_acknowledges() -> None:
    resume_calls: list[None] = []

    async with (
        httpx.AsyncClient(base_url=TELEGRAM_API_BASE) as client,
        respx.mock(base_url=TELEGRAM_API_BASE) as mock,
    ):
        send_route = mock.post(f"/bot{TOKEN}/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True}),
        )
        handler = _handler_with_control(
            _empty_state(), client, on_resume=lambda: resume_calls.append(None)
        )
        await handler._dispatch(
            {
                "update_id": 1,
                "message": {"chat": {"id": int(ALLOWED)}, "text": "/resume"},
            }
        )

        assert len(resume_calls) == 1
        body = json.loads(send_route.calls[0].request.read())
        assert "재개" in body["text"]


async def test_dispatch_stop_invokes_callback_and_acknowledges() -> None:
    stop_calls: list[None] = []

    async with (
        httpx.AsyncClient(base_url=TELEGRAM_API_BASE) as client,
        respx.mock(base_url=TELEGRAM_API_BASE) as mock,
    ):
        send_route = mock.post(f"/bot{TOKEN}/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True}),
        )
        handler = _handler_with_control(
            _empty_state(), client, on_stop=lambda: stop_calls.append(None)
        )
        await handler._dispatch(
            {
                "update_id": 1,
                "message": {"chat": {"id": int(ALLOWED)}, "text": "/stop"},
            }
        )

        assert len(stop_calls) == 1
        body = json.loads(send_route.calls[0].request.read())
        assert "종료 요청" in body["text"]


async def test_dispatch_pause_disabled_when_callback_not_provided() -> None:
    """on_pause 미주입 → '비활성' 응답, 콜백 호출 0회 (None 호출 시도 X)."""
    async with (
        httpx.AsyncClient(base_url=TELEGRAM_API_BASE) as client,
        respx.mock(base_url=TELEGRAM_API_BASE) as mock,
    ):
        send_route = mock.post(f"/bot{TOKEN}/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True}),
        )
        handler = _handler(_empty_state(), client)  # 콜백 X
        await handler._dispatch(
            {
                "update_id": 1,
                "message": {"chat": {"id": int(ALLOWED)}, "text": "/pause"},
            }
        )

        body = json.loads(send_route.calls[0].request.read())
        assert "비활성" in body["text"]


def test_format_status_includes_control_state_label() -> None:
    """control_state 가 한국어 라벨로 변환되어 status 출력에 들어가야 한다."""
    paused = _empty_state(control_state="paused")
    text = format_status(paused, now=datetime(2026, 4, 25, 9, 30))
    assert "일시정지" in text

    stopping = _empty_state(control_state="stopping")
    text = format_status(stopping, now=datetime(2026, 4, 25, 9, 30))
    assert "종료중" in text


def test_help_text_lists_v3_commands() -> None:
    assert "/pause" in HELP_TEXT
    assert "/resume" in HELP_TEXT
    assert "/stop" in HELP_TEXT


# ---- run() 루프 에러 분류 (cmd_poll_failed 노이즈 정리) -------------------


class _LogSpy:
    """structlog.get_logger 가 반환하는 객체를 흉내내는 스파이.

    debug/info/warning 호출을 (event, kwargs) 튜플로 모은다.
    """

    def __init__(self) -> None:
        self.debug_calls: list[tuple[str, dict[str, object]]] = []
        self.info_calls: list[tuple[str, dict[str, object]]] = []
        self.warning_calls: list[tuple[str, dict[str, object]]] = []

    def debug(self, event: str, **kwargs: object) -> None:
        self.debug_calls.append((event, kwargs))

    def info(self, event: str, **kwargs: object) -> None:
        self.info_calls.append((event, kwargs))

    def warning(self, event: str, **kwargs: object) -> None:
        self.warning_calls.append((event, kwargs))


def test_describe_exc_falls_back_to_repr_when_str_is_empty() -> None:
    """str(exc) 가 빈 문자열인 httpx 예외도 의미있는 라벨로 바뀌어야 한다."""
    from auto_trading.notify.telegram_commands import _describe_exc

    # ReadTimeout("") 처럼 stringify 시 빈 문자열인 예외 → repr 폴백.
    timeout = httpx.ReadTimeout("")
    assert str(timeout) == ""
    described = _describe_exc(timeout)
    assert described != ""
    assert "ReadTimeout" in described

    # 일반 예외는 그대로.
    assert _describe_exc(ValueError("boom")) == "boom"


async def test_run_loop_demotes_transport_error_to_debug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ReadTimeout 같은 httpx.TransportError 는 cmd_poll_failed warning 이 아니라
    cmd_poll_transient debug 로 기록된다 — 이게 본 PR 의 노이즈 정리 핵심."""
    from auto_trading.notify import telegram_commands as mod

    async def _no_sleep(_d: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    spy = _LogSpy()
    monkeypatch.setattr(mod, "log", spy)

    handler = TelegramCommandHandler(
        token=TOKEN,
        allowed_chat_ids={ALLOWED},
        state_provider=_empty_state,
        poll_timeout=1,
    )
    n = {"calls": 0}

    async def _fake_poll() -> list[dict[str, object]]:
        n["calls"] += 1
        if n["calls"] == 1:
            raise httpx.ReadTimeout("")  # 운영에서 관측된 빈-메시지 패턴
        raise asyncio.CancelledError

    monkeypatch.setattr(handler, "_poll", _fake_poll)

    with pytest.raises(asyncio.CancelledError):
        await handler.run()
    await handler.aclose()

    transient = [c for c in spy.debug_calls if c[0] == "notify.cmd_poll_transient"]
    poll_failed = [c for c in spy.warning_calls if c[0] == "notify.cmd_poll_failed"]
    assert transient, "transport timeout 은 debug 로그를 남겨야 함"
    assert not poll_failed, "transport timeout 은 warning 으로 찍히면 안 됨"
    _, kwargs = transient[0]
    assert kwargs["error_type"] == "ReadTimeout"
    assert kwargs["error"]
    assert kwargs["error"] != ""


async def test_run_loop_warns_with_repr_fallback_on_empty_str_exc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """transport 외 예측 못한 예외는 warning, str(exc) 가 비어있어도 error 필드는 비지 않아야 한다."""
    from auto_trading.notify import telegram_commands as mod

    async def _no_sleep(_d: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    spy = _LogSpy()
    monkeypatch.setattr(mod, "log", spy)

    class _SilentBoom(Exception):
        """str() 이 빈 문자열인 비-transport 예외."""

    handler = TelegramCommandHandler(
        token=TOKEN,
        allowed_chat_ids={ALLOWED},
        state_provider=_empty_state,
        poll_timeout=1,
    )
    n = {"calls": 0}

    async def _fake_poll() -> list[dict[str, object]]:
        n["calls"] += 1
        if n["calls"] == 1:
            raise _SilentBoom
        raise asyncio.CancelledError

    monkeypatch.setattr(handler, "_poll", _fake_poll)

    with pytest.raises(asyncio.CancelledError):
        await handler.run()
    await handler.aclose()

    failed = [c for c in spy.warning_calls if c[0] == "notify.cmd_poll_failed"]
    assert failed, "transport 외 예외는 여전히 warning 이어야 함"
    _, kwargs = failed[0]
    assert kwargs["error_type"] == "_SilentBoom"
    assert kwargs["error"] != ""
    assert "_SilentBoom" in str(kwargs["error"])
