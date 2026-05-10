"""텔레그램 인커밍 명령 핸들러 (long-poll).

흐름:
1. `getUpdates?offset=&timeout=30` 으로 long-poll
2. 메시지가 화이트리스트(`allowed_chat_ids`) 에 있는 chat 에서 왔는지 확인
3. `/status` `/balance` `/positions` `/pnl` `/last_signal` `/help` 디스패치
4. 응답을 같은 chat 으로 sendMessage

실패 격리:
- 어떤 예외도 트레이딩 핫패스(러너) 로 전파시키지 않는다 — 모두 swallow + log.
- 화이트리스트 미통과 → 응답 없이 무시 (봇 존재 노출 방지).

명령별 분기·LLM cooldown 은 `commands/` 패키지로 분리 — 본 모듈은 long-poll
루프 + 화이트리스트 + dispatch 위임만 담당.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import structlog

from .._common import mask_secret
from .commands import (
    CommandContext,
    ControlAction,
    InsightAction,
    InsightCooldown,
    StateProvider,
    UnpauseAction,
    dispatch_sync,
)
from .telegram import TELEGRAM_API_BASE

log = structlog.get_logger(__name__)


def _describe_exc(exc: BaseException) -> str:
    """str(exc) 가 비어있는 httpx 예외(ReadTimeout 등) 까지 의미있는 라벨로."""
    return str(exc) or repr(exc)


class TelegramCommandHandler:
    """텔레그램 봇으로 들어오는 슬래시 명령을 처리하는 long-poll 핸들러.

    on_pause/on_resume/on_stop 은 선택. 안 넘기면 해당 명령은 "비활성" 응답.
    러너의 동기 메서드(`runner.pause` 등)를 그대로 바인딩해 넘기는 게 표준 사용법.
    """

    def __init__(
        self,
        token: str,
        allowed_chat_ids: set[str],
        state_provider: StateProvider,
        client: httpx.AsyncClient | None = None,
        poll_timeout: int = 30,
        *,
        on_pause: ControlAction | None = None,
        on_resume: ControlAction | None = None,
        on_stop: ControlAction | None = None,
        on_unpause: UnpauseAction | None = None,
        on_insight: InsightAction | None = None,
        insight_cooldown_sec: int = 300,
    ) -> None:
        if not token:
            raise ValueError("TelegramCommandHandler 는 token 이 필요")
        if not allowed_chat_ids:
            raise ValueError("TelegramCommandHandler 는 allowed_chat_ids 가 비어있을 수 없습니다")
        self._token = token
        self._allowed = {str(c) for c in allowed_chat_ids}
        # long-poll 은 timeout 보다 약간 길게 (네트워크 여유)
        http_timeout = poll_timeout + 5
        self._client = client or httpx.AsyncClient(base_url=TELEGRAM_API_BASE, timeout=http_timeout)
        self._owns_client = client is None
        self._poll_timeout = poll_timeout
        self._offset: int = 0
        self._ctx = CommandContext(
            state_provider=state_provider,
            on_pause=on_pause,
            on_resume=on_resume,
            on_stop=on_stop,
            on_unpause=on_unpause,
        )
        self._insight = InsightCooldown(on_insight, cooldown_sec=insight_cooldown_sec)

    def __repr__(self) -> str:
        return f"TelegramCommandHandler(token={mask_secret(self._token)!r}, allowed={len(self._allowed)})"

    async def run(self) -> None:
        """무한 폴링 루프. CancelledError 가 오면 정상 종료."""
        log.info("notify.cmd_loop_started", allowed_count=len(self._allowed))
        try:
            while True:
                try:
                    updates = await self._poll()
                except httpx.TransportError as exc:
                    # long-poll 중 타임아웃·일시 네트워크 끊김은 정상 운영 일부 —
                    # warning 으로 띄우면 로그가 시끄러워서 debug 로 강등.
                    log.debug(
                        "notify.cmd_poll_transient",
                        error_type=type(exc).__name__,
                        error=_describe_exc(exc),
                    )
                    await asyncio.sleep(2.0)
                    continue
                except Exception as exc:
                    # 그 외 예측 못한 에러만 warning — 트레이딩은 막지 않음.
                    log.warning(
                        "notify.cmd_poll_failed",
                        error_type=type(exc).__name__,
                        error=_describe_exc(exc),
                    )
                    await asyncio.sleep(2.0)
                    continue
                for update in updates:
                    await self._dispatch(update)
        finally:
            log.info("notify.cmd_loop_stopped")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ---- 내부 ----------------------------------------------------------

    async def _poll(self) -> list[dict[str, Any]]:
        path = f"/bot{self._token}/getUpdates"
        # Telegram 은 allowed_updates 를 JSON 문자열로 받는다.
        params: dict[str, Any] = {
            "timeout": self._poll_timeout,
            "allowed_updates": json.dumps(["message"]),
        }
        if self._offset:
            params["offset"] = self._offset
        resp = await self._client.get(path, params=params)
        if resp.status_code >= 400:
            log.warning(
                "notify.cmd_poll_http_error",
                status=resp.status_code,
                body=resp.text[:200],
            )
            return []
        body = resp.json()
        if not body.get("ok"):
            log.warning("notify.cmd_poll_not_ok", desc=str(body.get("description"))[:200])
            return []
        result = body.get("result") or []
        if result:
            # 다음 폴링부터는 마지막 update_id + 1 부터.
            self._offset = max(int(u.get("update_id", 0)) for u in result) + 1
        return result

    async def _dispatch(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return
        chat_id_str = str(chat_id)

        if chat_id_str not in self._allowed:
            log.warning("notify.unauthorized", chat_id=chat_id_str)
            return

        text = (message.get("text") or "").strip()
        if not text:
            return
        # 명령은 첫 토큰만 본다 (`/foo@botname args` → `/foo`).
        cmd_token = text.split()[0]
        cmd = cmd_token.split("@", 1)[0].lower()

        try:
            if cmd == "/insight":
                reply = await self._insight.handle()
            else:
                reply = dispatch_sync(cmd, self._ctx)
        except Exception as exc:
            log.warning(
                "notify.cmd_dispatch_failed",
                cmd=cmd,
                error_type=type(exc).__name__,
                error=_describe_exc(exc),
            )
            reply = "처리 중 오류가 발생했습니다."

        await self._send(chat_id_str, reply)

    async def _send(self, chat_id: str, text: str) -> None:
        path = f"/bot{self._token}/sendMessage"
        try:
            resp = await self._client.post(
                path,
                json={"chat_id": chat_id, "text": text},
            )
            if resp.status_code >= 400:
                log.warning(
                    "notify.cmd_send_failed",
                    status=resp.status_code,
                    body=resp.text[:200],
                )
        except Exception as exc:
            log.warning(
                "notify.cmd_send_failed",
                error_type=type(exc).__name__,
                error=_describe_exc(exc),
            )
