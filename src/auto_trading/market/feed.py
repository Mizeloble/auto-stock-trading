"""WS 메시지를 도메인 이벤트(Tick, Bar) 로 변환."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from typing import AsyncIterator
from zoneinfo import ZoneInfo

from ..kiwoom.ws import KiwoomWebSocket

KST = ZoneInfo("Asia/Seoul")


@dataclass(frozen=True)
class Tick:
    symbol: str
    ts: datetime
    price: Decimal
    volume: int


def _d(raw: object) -> Decimal:
    if raw is None:
        return Decimal(0)
    s = str(raw).replace(",", "").replace("+", "").strip()
    if s in ("", "-"):
        return Decimal(0)
    try:
        return abs(Decimal(s))
    except Exception:
        return Decimal(0)


def _parse_ts(raw: str | None) -> datetime:
    if not raw:
        return datetime.now(tz=KST)
    raw = str(raw)
    try:
        if len(raw) == 6:  # HHMMSS
            t = time(int(raw[:2]), int(raw[2:4]), int(raw[4:6]))
            return datetime.combine(datetime.now(tz=KST).date(), t, tzinfo=KST)
        if len(raw) == 14:
            return datetime.strptime(raw, "%Y%m%d%H%M%S").replace(tzinfo=KST)
    except ValueError:
        pass
    return datetime.now(tz=KST)


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
            ts = _parse_ts(values.get("20"))     # 체결시간 HHMMSS
            price = _d(values.get("10"))         # 현재가
            volume = int(_d(values.get("15")))   # 체결거래량 (부호)
            yield Tick(symbol=symbol, ts=ts, price=price, volume=volume)
