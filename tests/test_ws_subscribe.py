"""KiwoomWebSocket._subscribe 재시도 + _run_loop 끊김 로깅 검증 (네트워크 없음)."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
import structlog
from websockets.exceptions import ConnectionClosed

from auto_trading.kiwoom import ws as ws_module
from auto_trading.kiwoom.ws import KiwoomWebSocket, Subscription


class _FakeWS:
    def __init__(self, fails: int = 0, exc: Exception | None = None) -> None:
        self._fails_left = fails
        self._exc = exc or RuntimeError("transient")
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        if self._fails_left > 0:
            self._fails_left -= 1
            raise self._exc
        self.sent.append(payload)


def _client() -> KiwoomWebSocket:
    ws = KiwoomWebSocket.__new__(KiwoomWebSocket)
    ws._subscriptions = [Subscription(symbols=["005930"], types=["0B"])]
    return ws


async def test_subscribe_retries_on_transient_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _noop(_s: float) -> None:
        return None

    monkeypatch.setattr("auto_trading.kiwoom.ws.asyncio.sleep", _noop)
    client = _client()
    fake = _FakeWS(fails=2)

    await client._subscribe(fake)

    assert len(fake.sent) == 1
    msg = json.loads(fake.sent[0])
    assert msg["trnm"] == "REG"
    assert msg["data"][0]["item"] == ["005930"]


async def test_subscribe_raises_after_exhausting_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _noop(_s: float) -> None:
        return None

    monkeypatch.setattr("auto_trading.kiwoom.ws.asyncio.sleep", _noop)
    client = _client()
    fake = _FakeWS(fails=10)

    with pytest.raises(RuntimeError, match="transient"):
        await client._subscribe(fake)


async def test_subscribe_propagates_connection_closed_immediately() -> None:
    client = _client()
    fake = _FakeWS(fails=1, exc=ConnectionClosed(None, None))

    with pytest.raises(ConnectionClosed):
        await client._subscribe(fake)
    # 연결 자체가 끊기면 재접속 루프에 맡겨야 하므로 재시도 없이 즉시 전파
    assert fake._fails_left == 0


# --- _run_loop: 끊김 로깅 분기 검증 ----------------------------------------------------


class _ConnCtx:
    """`async with websockets.connect(...) as ws:` 형태를 흉내내는 컨텍스트."""

    def __init__(
        self,
        on_iter: Any,
        captured_kwargs: dict[str, Any],
        kwargs: dict[str, Any],
    ) -> None:
        self._on_iter = on_iter
        captured_kwargs.update(kwargs)
        self.sent: list[str] = []

    async def __aenter__(self) -> _ConnCtx:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def recv(self) -> str:
        return json.dumps({"trnm": "LOGIN", "return_code": 0})

    def __aiter__(self) -> _ConnCtx:
        return self

    async def __anext__(self) -> str:
        # 호출 측에서 주입한 정책에 따라 yield 또는 raise.
        return await self._on_iter(self)


def _patch_run_loop_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    on_iter: Any,
    captured_kwargs: dict[str, Any],
    fake_loop_time: list[float],
) -> KiwoomWebSocket:
    """_run_loop 가 외부 의존(토큰/소켓/시간/sleep) 없이 돌게 모킹."""

    def _connect(_url: str, **kwargs: Any) -> _ConnCtx:
        return _ConnCtx(on_iter=on_iter, captured_kwargs=captured_kwargs, kwargs=kwargs)

    monkeypatch.setattr(ws_module.websockets, "connect", _connect)

    async def _no_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr(ws_module.asyncio, "sleep", _no_sleep)

    class _FakeLoop:
        def time(self) -> float:
            return fake_loop_time[0]

    monkeypatch.setattr(ws_module.asyncio, "get_event_loop", lambda: _FakeLoop())

    client = KiwoomWebSocket.__new__(KiwoomWebSocket)
    client._settings = type("S", (), {"ws_url": "wss://example/ws"})()
    client._token_provider = AsyncMock()
    client._token_provider.get_token = AsyncMock(return_value="tok")
    client._queue = asyncio.Queue()
    client._conn = None
    client._subscriptions = [Subscription(symbols=["005930"], types=["0B"])]
    client._task = None
    client._stop = asyncio.Event()
    return client


async def test_run_loop_passes_relaxed_keepalive_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """websockets.connect 호출 시 ping_interval=60, ping_timeout=30 으로 완화."""
    captured: dict[str, Any] = {}
    fake_time = [0.0]

    async def _iter_then_stop(_ws: _ConnCtx) -> str:
        # 첫 iteration 에서 stop 신호 → 재접속 루프 종료.
        client._stop.set()
        raise StopAsyncIteration

    client = _patch_run_loop_env(
        monkeypatch,
        on_iter=_iter_then_stop,
        captured_kwargs=captured,
        fake_loop_time=fake_time,
    )

    await client._run_loop()

    assert captured["ping_interval"] == ws_module._WS_PING_INTERVAL == 60
    assert captured["ping_timeout"] == ws_module._WS_PING_TIMEOUT == 30


def _disconnect_entries(captured: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in captured if e.get("event") == "ws.disconnect"]


async def test_run_loop_logs_short_disconnect_as_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """연결 직후(< 임계) 끊기면 진짜 문제 신호이므로 WARNING 으로 기록."""
    captured: dict[str, Any] = {}
    fake_time = [1000.0]
    raised = {"count": 0}

    async def _drop_quickly(_ws: _ConnCtx) -> str:
        if raised["count"] == 0:
            # 연결 직후(uptime≈0) ConnectionResetError 로 끊김.
            raised["count"] += 1
            raise ConnectionResetError(54, "Connection reset by peer")
        client._stop.set()
        raise StopAsyncIteration

    client = _patch_run_loop_env(
        monkeypatch,
        on_iter=_drop_quickly,
        captured_kwargs=captured,
        fake_loop_time=fake_time,
    )

    with structlog.testing.capture_logs() as cap:
        await client._run_loop()

    disconnects = _disconnect_entries(cap)
    assert disconnects, "ws.disconnect 로그가 한 번은 떠야 한다"
    assert disconnects[0]["log_level"] == "warning"
    assert disconnects[0]["uptime_seconds"] == 0.0


async def test_run_loop_logs_long_idle_disconnect_as_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """모의서버 idle 타임아웃(임계 이상 유지 후 끊김)은 INFO 로 강등돼 노이즈를 줄인다."""
    captured: dict[str, Any] = {}
    # connected_at = 1000 → drop 시점 1000 + 360 = 1360 (≈ 6 분 유지 후 끊김)
    fake_time = [1000.0]
    raised = {"count": 0}

    async def _drop_after_long_uptime(_ws: _ConnCtx) -> str:
        if raised["count"] == 0:
            raised["count"] += 1
            fake_time[0] = 1360.0  # uptime ≈ 360s
            raise ConnectionResetError(54, "Connection reset by peer")
        client._stop.set()
        raise StopAsyncIteration

    client = _patch_run_loop_env(
        monkeypatch,
        on_iter=_drop_after_long_uptime,
        captured_kwargs=captured,
        fake_loop_time=fake_time,
    )

    with structlog.testing.capture_logs() as cap:
        await client._run_loop()

    disconnects = _disconnect_entries(cap)
    assert disconnects, "ws.disconnect 로그가 한 번은 떠야 한다"
    assert disconnects[0]["log_level"] == "info"
    assert disconnects[0]["uptime_seconds"] >= ws_module._EXPECTED_IDLE_DISCONNECT_SECONDS
    # WARNING 으로 잘못 떨어지는 회귀가 없는지 함께 확인
    assert all(e["log_level"] != "warning" for e in disconnects)


async def test_signal_stop_unblocks_messages_within_one_second() -> None:
    """signal_stop() 후 1초 안에 messages() 가 종료돼야 — main loop hang 방지."""
    ws = KiwoomWebSocket.__new__(KiwoomWebSocket)
    ws._stop = asyncio.Event()
    ws._queue = asyncio.Queue()

    # 큐가 비어 있는 상태에서 messages() 가 1초 폴링 루프에 진입.
    aiter = ws.messages().__aiter__()

    async def consume() -> None:
        async for _ in aiter:
            return  # 메시지 오면 즉시 종료
        # 메시지 없이 _stop 으로 종료된 경우 여기 도달

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0.1)
    assert not consumer.done(), "stop 신호 전엔 messages() 가 대기 상태여야 함"

    ws.signal_stop()  # 시그널만 발사 (sync, await 없음)

    # ws.messages() 의 1초 timeout 다음 사이클에서 _stop 보고 종료해야 함.
    await asyncio.wait_for(consumer, timeout=2.0)
    assert ws._stop.is_set()
