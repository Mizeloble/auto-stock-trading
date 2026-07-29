"""전략 베이스 및 공통 도메인 타입.

**중요**: 전략은 외부 부작용(네트워크·파일·시계) 없이 입력 Bar 만 보고 판단해야 한다.
그래야 백테스트 결과가 재현 가능하다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from .._common import Bar, Side

__all__ = ["Bar", "Side", "Signal", "Strategy"]


@dataclass(frozen=True)
class Signal:
    """전략이 내놓는 매매 의도.

    - `side`: 매수/매도
    - `symbol`: 대상 종목
    - `weight`: 0.0~1.0. 종목 단위 목표 비중 (최종 수량은 리스크 매니저가 결정)
    - `reason`: 로그/리포트용 설명 문자열
    """

    symbol: str
    side: Side
    weight: float = 1.0
    reason: str = ""
    ts: datetime | None = None
    price_hint: Decimal | None = None


class Strategy(ABC):
    """모든 전략의 공통 인터페이스."""

    #: CLI 에서 `--strategy` 로 쓰일 이름
    name: str = ""

    def __init__(self, **params: Any) -> None:
        self.params: dict[str, Any] = dict(params)
        self.state: dict[str, Any] = {}

    @abstractmethod
    def on_bar(self, bar: Bar) -> list[Signal]:
        """새 봉이 확정되면 호출. 매매 의도를 반환."""

    def on_tick(self, bar: Bar) -> list[Signal]:
        """틱 단위 업데이트(옵션). 기본은 아무 것도 안 함."""
        return []

    def reset(self) -> None:
        """백테스트/세션 재시작 시 상태 초기화."""
        self.state.clear()
