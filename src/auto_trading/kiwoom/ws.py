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
import json
from dataclasses import dataclass, field
from typing import AsyncIterator

import structlog
import websockets
from websockets.exceptions import ConnectionClosed

from ..config import Settings, get_settings
from .auth import TokenProvider

log = structlog.get_logger(__name__)


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
            try:
                await self._conn.close()
            except Exception:  # pragma: no cover
                pass
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
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
        while not self._stop.is_set():
            try:
                token = await self._token_provider.get_token()
                async with websockets.connect(self._settings.ws_url, ping_interval=30) as ws:
                    self._conn = ws
                    await self._handshake(ws, token)
                    await self._subscribe(ws)
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
                log.warning("ws.disconnect", error=str(exc), backoff=backoff)
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
            await ws.send(json.dumps(payload))
            log.info("ws.subscribe", symbols=sub.symbols, types=sub.types)
