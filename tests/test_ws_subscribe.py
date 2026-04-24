"""KiwoomWebSocket._subscribe 재시도 동작 검증 (네트워크 없음)."""

from __future__ import annotations

import json

import pytest
from websockets.exceptions import ConnectionClosed

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
