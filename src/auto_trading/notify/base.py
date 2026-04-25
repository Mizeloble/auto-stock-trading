"""알림 프로토콜 + Noop 기본 구현.

러너는 `Notifier` 만 알면 된다. 백엔드(텔레그램·슬랙·...) 가 무엇이든 시그니처 동일.
미설정 환경에서는 `NoopNotifier` 가 자동 주입돼 외부 호출이 발생하지 않는다.
"""

from __future__ import annotations

from typing import Protocol


class Notifier(Protocol):
    """이벤트 푸시 백엔드 프로토콜.

    `event` 는 structlog 이벤트명과 동일 컨벤션(`<module>.<event>`).
    `fields` 는 `log.info(event, **fields)` 와 동일 — 백엔드에서 사람이 읽을 수 있는
    메시지로 포맷팅한다.
    """

    async def notify(self, event: str, **fields: object) -> None: ...

    async def aclose(self) -> None: ...


class NoopNotifier:
    """텔레그램 등 키 미설정 시 폴백. 호출되어도 외부 통신 없음."""

    async def notify(self, event: str, **fields: object) -> None:
        return None

    async def aclose(self) -> None:
        return None
