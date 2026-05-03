"""텔레그램 봇 알림 백엔드.

핵심 정책:
- 토큰·chat_id 는 어떤 출력에도 노출되지 않는다 (앞 4자 + `…` 마스킹).
- sendMessage 실패는 로그만 남기고 swallow — 알림이 트레이딩을 멈추면 안 된다.
- HTTP 클라이언트는 키움과 별도 (`https://api.telegram.org`).
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"


def _mask(token: str) -> str:
    return token[:4] + "…" if token else ""


def _format_message(event: str, fields: dict[str, Any]) -> str:
    """이벤트명 + kwargs 를 사람이 읽을 한 줄 메시지로."""
    if event == "runner.started":
        strategy = fields.get("strategy", "?")
        symbols = fields.get("symbols", "?")
        dry = " (dry-run)" if fields.get("dry_run") else ""
        return f"▶️ paper 시작{dry}\n전략: {strategy}\n종목: {symbols}"
    if event == "runner.stopped":
        reason = fields.get("reason", "정상")
        msg = f"⏹ paper 정지 ({reason})"
        # day-trade 모드의 자기 종료(universe 비어있음 / 발굴 실패) 면 사용자가 그 후
        # /status 같은 명령을 보내도 듣는 프로세스 없음 — 무응답으로 끝나 헷갈림.
        # 한 줄 안내 추가 (5/3 사용자 피드백).
        if "before_universe" in str(reason) or "no_candidates" in str(reason):
            msg += "\n💡 다음 시동(평일 09:00)까지 텔레그램 명령 비활성"
        return msg
    if event == "runner.order_accepted":
        return (
            f"✅ 체결 접수 {fields.get('symbol', '?')} {str(fields.get('side', '?')).upper()}"
            f" {fields.get('qty', '?')}주"
            f" (ord:{fields.get('order_no', '-')})"
        )
    if event == "runner.order_failed":
        return (
            f"❌ 주문 실패 {fields.get('symbol', '?')} {str(fields.get('side', '?')).upper()}"
            f" {fields.get('qty', '?')}주: {fields.get('error', 'unknown')}"
        )
    if event == "runner.balance_failed_fatal":
        return f"🛑 잔고 조회 {fields.get('consecutive', '?')}회 연속 실패 — 러너 중단"

    # ---- 21단계 day-trade 이벤트 ----------------------------------------

    if event == "daytrade.discovery_done":
        chosen = fields.get("chosen") or []
        blocked = fields.get("blocked") or []
        note = fields.get("note") or ""
        chosen_str = ", ".join(str(s) for s in chosen) if chosen else "없음"
        head = f"🔎 발굴 완료 · 매수 후보 {len(chosen)}건"
        body = f"\n선정: {chosen_str}"
        if blocked:
            blocked_summary = ", ".join(f"{sym}({reason})" for sym, reason in blocked[:5])
            if len(blocked) > 5:
                blocked_summary += f" 외 {len(blocked) - 5}"
            body += f"\n차단: {blocked_summary}"
        if note:
            body += f"\n메모: {note}"
        return head + body

    if event == "runner.liquidate_done":
        count = fields.get("count", 0)
        reason = fields.get("reason", "?")
        dry = " (dry-run)" if fields.get("dry_run") else ""
        return f"🧹 청산 완료{dry} · {count}종목 ({reason})"

    if event == "runner.liquidate_failed":
        return (
            f"❌ 청산 실패 {fields.get('symbol', '?')} {fields.get('qty', '?')}주: "
            f"{fields.get('error', 'unknown')}"
        )

    # 미정의 이벤트는 그대로 한 줄로
    parts = [f"{k}={v}" for k, v in fields.items()]
    return f"[{event}] " + " ".join(parts)


class TelegramNotifier:
    """텔레그램 봇 sendMessage 래퍼. fire-and-forget 운용 권장."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        client: httpx.AsyncClient | None = None,
        timeout: float = 5.0,
    ) -> None:
        if not token or not chat_id:
            raise ValueError("TelegramNotifier 는 token + chat_id 둘 다 필요")
        self._token = token
        self._chat_id = chat_id
        self._client = client or httpx.AsyncClient(base_url=TELEGRAM_API_BASE, timeout=timeout)
        self._owns_client = client is None

    def __repr__(self) -> str:
        return f"TelegramNotifier(token={_mask(self._token)!r}, chat_id={self._chat_id!r})"

    async def notify(self, event: str, **fields: object) -> None:
        text = _format_message(event, fields)
        path = f"/bot{self._token}/sendMessage"
        try:
            resp = await self._client.post(
                path,
                json={"chat_id": self._chat_id, "text": text},
            )
            if resp.status_code >= 400:
                log.warning(
                    "notify.send_failed",
                    for_event=event,
                    status=resp.status_code,
                    body=resp.text[:200],
                )
        except Exception as e:
            # 네트워크/타임아웃 — 트레이딩 막지 않도록 swallow
            log.warning("notify.send_failed", for_event=event, error=str(e))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
