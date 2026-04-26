"""텔레그램 인커밍 명령 핸들러 (long-poll).

흐름:
1. `getUpdates?offset=&timeout=30` 으로 long-poll
2. 메시지가 화이트리스트(`allowed_chat_ids`) 에 있는 chat 에서 왔는지 확인
3. `/status` `/balance` `/positions` `/pnl` `/last_signal` `/help` 디스패치
4. 응답을 같은 chat 으로 sendMessage

실패 격리:
- 어떤 예외도 트레이딩 핫패스(러너) 로 전파시키지 않는다 — 모두 swallow + log.
- 화이트리스트 미통과 → 응답 없이 무시 (봇 존재 노출 방지).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx
import structlog

from .state import (
    HELP_TEXT,
    RunnerStateSnapshot,
    format_balance,
    format_last_signal,
    format_pnl,
    format_positions,
    format_status,
)
from .telegram import TELEGRAM_API_BASE, _mask

log = structlog.get_logger(__name__)


StateProvider = Callable[[], RunnerStateSnapshot]


class TelegramCommandHandler:
    """텔레그램 봇으로 들어오는 슬래시 명령을 처리하는 long-poll 핸들러."""

    def __init__(
        self,
        token: str,
        allowed_chat_ids: set[str],
        state_provider: StateProvider,
        client: httpx.AsyncClient | None = None,
        poll_timeout: int = 30,
    ) -> None:
        if not token:
            raise ValueError("TelegramCommandHandler 는 token 이 필요")
        if not allowed_chat_ids:
            raise ValueError("TelegramCommandHandler 는 allowed_chat_ids 가 비어있을 수 없습니다")
        self._token = token
        self._allowed = {str(c) for c in allowed_chat_ids}
        self._state_provider = state_provider
        # long-poll 은 timeout 보다 약간 길게 (네트워크 여유)
        http_timeout = poll_timeout + 5
        self._client = client or httpx.AsyncClient(base_url=TELEGRAM_API_BASE, timeout=http_timeout)
        self._owns_client = client is None
        self._poll_timeout = poll_timeout
        self._offset: int = 0

    def __repr__(self) -> str:
        return f"TelegramCommandHandler(token={_mask(self._token)!r}, allowed={len(self._allowed)})"

    async def run(self) -> None:
        """무한 폴링 루프. CancelledError 가 오면 정상 종료."""
        log.info("notify.cmd_loop_started", allowed_count=len(self._allowed))
        try:
            while True:
                try:
                    updates = await self._poll()
                except Exception as exc:
                    # 네트워크 오류는 짧게 쉬고 재시도 — 트레이딩을 막지 않음.
                    log.warning("notify.cmd_poll_failed", error=str(exc))
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
            reply = self._handle_command(cmd)
        except Exception as exc:
            log.warning("notify.cmd_dispatch_failed", cmd=cmd, error=str(exc))
            reply = "처리 중 오류가 발생했습니다."

        await self._send(chat_id_str, reply)

    def _handle_command(self, cmd: str) -> str:
        if cmd in ("/start", "/help"):
            return HELP_TEXT
        state = self._state_provider()
        if cmd == "/status":
            return format_status(state, now=datetime.now())
        if cmd == "/balance":
            return format_balance(state)
        if cmd == "/positions":
            return format_positions(state)
        if cmd == "/pnl":
            return format_pnl(state)
        if cmd == "/last_signal":
            return format_last_signal(state)
        return HELP_TEXT

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
            log.warning("notify.cmd_send_failed", error=str(exc))
