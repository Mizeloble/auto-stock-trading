"""다채널 알림 fan-out — 여러 notifier 에 동시 발송.

29단계 PR25 (#12 1차) — 한 채널 다운 시 알림 침묵 위험 방어. 모든 채널에
동일 메시지를 보내고 *한 채널이라도* 도달하면 운영자가 인지 가능.

설계 — primary/fallback 분리 (한 채널 실패 시 다른 채널 시도) 대신 *fan-out*:
- TelegramNotifier 가 이미 swallow + log — 실패 여부를 호출자가 알 수 없음.
- 분기 로직 (failure detection) 을 도입하면 인터페이스 복잡. 모든 채널에
  보내는 단순 fan-out 이 *중복 메시지 < 채널 단일 다운* 트레이드오프.
- 텔레그램 spam 우려는 사용자가 환경 변수로 disable 가능 (DISCORD_WEBHOOK_URL
  비우면 자동 단일 채널).
"""

from __future__ import annotations

import structlog

from .base import Notifier

log = structlog.get_logger(__name__)


class MultiNotifier:
    """여러 notifier 에 fan-out. 모든 채널에 동일 이벤트 전달."""

    def __init__(self, notifiers: list[Notifier]) -> None:
        if not notifiers:
            raise ValueError("MultiNotifier 는 최소 1개의 notifier 필요")
        self._notifiers = notifiers

    def __repr__(self) -> str:
        names = [type(n).__name__ for n in self._notifiers]
        return f"MultiNotifier({', '.join(names)})"

    async def notify(self, event: str, **fields: object) -> None:
        for notifier in self._notifiers:
            try:
                await notifier.notify(event, **fields)
            except Exception as exc:
                # Notifier 들이 이미 내부에서 swallow 하지만 방어선 추가.
                log.warning(
                    "notify.multi_send_failed",
                    backend=type(notifier).__name__,
                    for_event=event,
                    error=str(exc),
                )

    async def aclose(self) -> None:
        for notifier in self._notifiers:
            try:
                await notifier.aclose()
            except Exception as exc:
                log.warning(
                    "notify.multi_aclose_failed",
                    backend=type(notifier).__name__,
                    error=str(exc),
                )
