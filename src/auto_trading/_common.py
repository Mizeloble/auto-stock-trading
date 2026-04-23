"""프로젝트 전반에서 공유하는 원시 타입·파서.

여러 모듈이 똑같이 작성하던 Decimal 파싱·타임스탬프 파싱·응답 리스트 추출 로직을
한 곳에 모은다. 도메인 의미가 아니라 "형식 처리" 수준의 공용 유틸만 둔다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


class Side(StrEnum):
    """매매 방향. 전략 시그널과 주문에서 공용."""

    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class Bar:
    """OHLCV 단일 봉. 전략·백테스트·러너에서 공용."""

    symbol: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


def to_decimal(raw: object) -> Decimal:
    """키움 응답 문자열(부호/콤마 포함 가능) → Decimal. 실패 시 0."""
    if raw is None:
        return Decimal(0)
    s = str(raw).replace(",", "").replace("+", "").strip()
    if s in ("", "-"):
        return Decimal(0)
    try:
        return Decimal(s)
    except Exception:
        return Decimal(0)


def parse_kiwoom_ts(raw: str | None) -> datetime:
    """키움 타임스탬프 파서. HHMMSS / YYYYMMDD / YYYYMMDDHHMMSS 지원.

    값이 비거나 해석 불가하면 현재 KST 시각을 반환한다.
    """
    if not raw:
        return datetime.now(tz=KST)
    s = str(raw).strip()
    try:
        if len(s) == 6:
            t = time(int(s[:2]), int(s[2:4]), int(s[4:6]))
            return datetime.combine(datetime.now(tz=KST).date(), t, tzinfo=KST)
        if len(s) == 8:
            return datetime.strptime(s, "%Y%m%d").replace(tzinfo=KST)
        if len(s) == 14:
            return datetime.strptime(s, "%Y%m%d%H%M%S").replace(tzinfo=KST)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(s).astimezone(KST)
    except ValueError:
        return datetime.now(tz=KST)


def extract_rows(payload: dict) -> list[dict]:
    """응답 dict 안에서 첫 번째 list[dict] 필드를 찾아 반환. 내부 키(`_…`)는 무시."""
    for key, value in payload.items():
        if isinstance(key, str) and key.startswith("_"):
            continue
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value
    return []


__all__ = ["KST", "Side", "Bar", "to_decimal", "parse_kiwoom_ts", "extract_rows"]
