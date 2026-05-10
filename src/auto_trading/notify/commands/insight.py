"""`/insight` LLM 호출 + cooldown 상태.

LLM 호출은 시간이 걸려 async, 그리고 비용·rate-limit 보호용 cooldown 상태를
인스턴스가 보유. 기본 5분 cooldown — 사용자가 빠르게 두 번 누르면 두 번째는
안내 메시지로 안전하게 거부.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime

import structlog

InsightAction = Callable[[], Awaitable[str]]

log = structlog.get_logger(__name__)


def _describe_exc(exc: BaseException) -> str:
    return str(exc) or repr(exc)


class InsightCooldown:
    """`/insight` 핸들러 + cooldown 상태 보유.

    `on_insight=None` 이면 비활성 봇 — `handle()` 이 안내 문자열만 반환.
    """

    def __init__(
        self,
        on_insight: InsightAction | None,
        *,
        cooldown_sec: int = 300,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._on_insight = on_insight
        self._cooldown_sec = cooldown_sec
        self._clock = clock
        self._last_at: datetime | None = None

    async def handle(self) -> str:
        if self._on_insight is None:
            return "이 봇 인스턴스에선 /insight 가 비활성입니다."
        now = self._clock()
        if self._last_at is not None:
            elapsed = (now - self._last_at).total_seconds()
            if elapsed < self._cooldown_sec:
                remaining = int(self._cooldown_sec - elapsed)
                return f"⏳ /insight 쿨다운 — {remaining}초 후 다시 시도하세요."
        # cooldown 갱신은 LLM 호출 직전에 — 호출 자체가 실패해도 다음 호출이
        # 즉시 또 LLM 으로 가지 않도록 (비용 보호).
        self._last_at = now
        try:
            return await self._on_insight()
        except Exception as exc:
            log.warning(
                "notify.insight_failed",
                error_type=type(exc).__name__,
                error=_describe_exc(exc),
            )
            return "🧭 인사이트 생성 실패 — 로그를 확인하세요."
