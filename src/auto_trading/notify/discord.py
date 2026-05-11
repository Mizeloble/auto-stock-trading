"""Discord 웹훅 알림 백엔드.

29단계 PR25 (#12 1차) — 텔레그램 외 fallback 채널. 둘 다 활성이면 모든 알림이
양쪽으로 fan-out (중복 알림이 한 채널 다운보다 안전).

핵심 정책:
- 웹훅 URL 자체가 시크릿 — 로그에 노출 X (앞 4자만).
- sendMessage 실패는 swallow + warning.
- 메시지 포맷팅은 `telegram._format_message` 재사용 — 이벤트→사람 표현 일관.
"""

from __future__ import annotations

import httpx
import structlog

from auto_trading._common import mask_secret

from .telegram import _format_message

log = structlog.get_logger(__name__)


class DiscordNotifier:
    """Discord incoming-webhook 래퍼. fire-and-forget.

    웹훅 발급: Discord 채널 설정 > Integrations > Webhooks > New Webhook.
    URL 형식: ``https://discord.com/api/webhooks/{id}/{token}``.
    """

    def __init__(
        self,
        webhook_url: str,
        client: httpx.AsyncClient | None = None,
        timeout: float = 5.0,
    ) -> None:
        if not webhook_url:
            raise ValueError("DiscordNotifier 는 webhook_url 이 필요")
        self._url = webhook_url
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None

    def __repr__(self) -> str:
        # URL 의 token 부분 마스킹.
        return f"DiscordNotifier(url={mask_secret(self._url)!r})"

    async def notify(self, event: str, **fields: object) -> None:
        text = _format_message(event, fields)
        try:
            resp = await self._client.post(self._url, json={"content": text})
            if resp.status_code >= 400:
                log.warning(
                    "notify.discord_send_failed",
                    for_event=event,
                    status=resp.status_code,
                    body=resp.text[:200],
                )
        except Exception as exc:
            # 네트워크/타임아웃 — 트레이딩 핫패스 보호.
            log.warning("notify.discord_send_failed", for_event=event, error=str(exc))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
