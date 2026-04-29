"""키움 WebSocket 클라이언트 (실시간 시세/체결).

연결 흐름 (키움 REST-API WS 규약 요지):
1. `wss://{host}:10000/api/dostk/websocket` 접속
2. 서버에서 `{"trnm": "LOGIN"}` 요구 → 클라이언트가 `{"trnm":"LOGIN","token": <토큰>}` 전송
3. 로그인 성공 응답 수신
4. 구독: `{"trnm":"REG", "grp_no":"1", "refresh":"1", "data":[{"item":[<종목코드>...], "type":[<0B,0D...>]}]}`
5. 이후 서버가 틱(체결/호가 등)을 지속 push — `trnm=REAL`

본 구현은 연결·재연결·구독·수신 큐잉까지만 담당한다. 파서는 `market/feed.py` 에서
도메인 타입(Tick/Quote)으로 변환.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import structlog
import websockets
from websockets.exceptions import ConnectionClosed

from ..config import Settings, get_settings
from .auth import TokenProvider

log = structlog.get_logger(__name__)

# 키움 모의서버(`mockapi.kiwoom.com`) 는 LOGIN/REG 후 약 5~6 분 idle 이면 close frame 없이
# TCP 를 끊는다. 이 시간 이상 유지된 뒤 끊긴 건 정상 재접속 사이클로 보고 INFO 로 기록해
# 운영 로그 노이즈를 줄인다. 짧게 끊기면 진짜 문제 신호로 WARNING 유지.
_EXPECTED_IDLE_DISCONNECT_SECONDS = 120.0

# WS keepalive 는 키움 서버에 다소 공격적이라 ping_interval/ping_timeout 을 완화.
# (라이브러리 기본 20s/20s → 이전 30s/20s → 60s/30s 로 여유)
_WS_PING_INTERVAL = 60
_WS_PING_TIMEOUT = 30


@dataclass
class Subscription:
    symbols: list[str]
    types: list[str] = field(default_factory=lambda: ["0B"])  # 0B = 주식체결
    group: str = "1"
    refresh: str = "1"


class KiwoomWebSocket:
    """재접속·로그인·구독을 알아서 처리하는 WS 래퍼."""

    def __init__(
        self,
        settings: Settings | None = None,
        token_provider: TokenProvider | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._token_provider = token_provider or TokenProvider(settings=self._settings)
        self._queue: asyncio.Queue[dict] = asyncio.Queue()
        self._conn: websockets.ClientConnection | None = None
        self._subscriptions: list[Subscription] = []
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self, subscriptions: list[Subscription]) -> None:
        self._subscriptions = subscriptions
        self._task = asyncio.create_task(self._run_loop(), name="kiwoom-ws")

    async def stop(self) -> None:
        self._stop.set()
        if self._conn is not None:
            with contextlib.suppress(Exception):  # pragma: no cover
                await self._conn.close()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except TimeoutError:
                self._task.cancel()
            except asyncio.CancelledError:
                self._task.cancel()
        await self._token_provider.aclose()

    async def messages(self) -> AsyncIterator[dict]:
        """수신 이벤트 스트림. 취소 가능."""
        while not self._stop.is_set():
            try:
                msg = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            yield msg

    async def _run_loop(self) -> None:
        backoff = 1.0
        loop = asyncio.get_event_loop()
        while not self._stop.is_set():
            connected_at: float | None = None
            try:
                token = await self._token_provider.get_token()
                async with websockets.connect(
                    self._settings.ws_url,
                    ping_interval=_WS_PING_INTERVAL,
                    ping_timeout=_WS_PING_TIMEOUT,
                ) as ws:
                    self._conn = ws
                    await self._handshake(ws, token)
                    await self._subscribe(ws)
                    connected_at = loop.time()
                    log.info("ws.connected", url=self._settings.ws_url)
                    backoff = 1.0
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if msg.get("trnm") == "PING":
                            await ws.send(raw)  # echo back
                            continue
                        await self._queue.put(msg)
            except (ConnectionClosed, OSError) as exc:
                if self._stop.is_set():
                    return
                uptime = loop.time() - connected_at if connected_at is not None else 0.0
                fields = {
                    "error": str(exc),
                    "uptime_seconds": round(uptime, 1),
                    "backoff": backoff,
                }
                if uptime >= _EXPECTED_IDLE_DISCONNECT_SECONDS:
                    # 모의서버 idle 타임아웃으로 추정 — 재접속 루프가 처리하므로 INFO.
                    log.info("ws.disconnect", **fields)
                else:
                    log.warning("ws.disconnect", **fields, exc_info=exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _handshake(self, ws, token: str) -> None:
        await ws.send(json.dumps({"trnm": "LOGIN", "token": token}))
        for _ in range(5):
            raw = await ws.recv()
            msg = json.loads(raw)
            if msg.get("trnm") == "LOGIN":
                if msg.get("return_code") not in (0, "0"):
                    raise RuntimeError(f"ws login failed: {msg}")
                return
        raise RuntimeError("ws login handshake timeout")

    async def _subscribe(self, ws) -> None:
        for sub in self._subscriptions:
            payload = {
                "trnm": "REG",
                "grp_no": sub.group,
                "refresh": sub.refresh,
                "data": [{"item": sub.symbols, "type": sub.types}],
            }
            delay = 1.0
            for attempt in range(1, 4):
                try:
                    await ws.send(json.dumps(payload))
                    log.info("ws.subscribe", symbols=sub.symbols, types=sub.types, attempt=attempt)
                    break
                except (ConnectionClosed, OSError) as exc:
                    # 연결 자체가 끊긴 경우: 재접속 루프가 처리하도록 그대로 전파
                    log.warning("ws.subscribe_connection_lost", error=str(exc))
                    raise
                except Exception as exc:
                    if attempt == 3:
                        log.error("ws.subscribe_failed", error=str(exc), symbols=sub.symbols)
                        raise
                    log.warning(
                        "ws.subscribe_retry",
                        error=str(exc),
                        symbols=sub.symbols,
                        attempt=attempt,
                        next_delay=delay,
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 5.0)
