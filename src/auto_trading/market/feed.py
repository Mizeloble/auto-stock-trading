"""WS 메시지를 도메인 이벤트(Tick) 로 변환."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .._common import parse_kiwoom_ts, to_decimal
from ..kiwoom.ws import KiwoomWebSocket


@dataclass(frozen=True)
class Tick:
    symbol: str
    ts: datetime
    price: Decimal
    volume: int


async def ticks(ws: KiwoomWebSocket) -> AsyncIterator[Tick]:
    """WS 메시지를 순회하며 체결(`0B`) 타입만 Tick 으로 변환해 스트리밍."""
    async for msg in ws.messages():
        if msg.get("trnm") != "REAL":
            continue
        for item in msg.get("data", []) or []:
            if item.get("type") != "0B":
                continue
            values = item.get("values") or {}
            symbol = str(item.get("item") or values.get("9001") or "").strip().lstrip("A")
            yield Tick(
                symbol=symbol,
                ts=parse_kiwoom_ts(values.get("20")),  # 체결시간 HHMMSS
                price=abs(to_decimal(values.get("10"))),  # 현재가
                volume=int(to_decimal(values.get("15"))),  # 체결거래량
            )
