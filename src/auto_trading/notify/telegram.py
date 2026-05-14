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

from auto_trading._common import mask_secret

log = structlog.get_logger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"


def _with_name(symbol: str, name: object) -> str:
    """`005930 삼성전자` 형식. 이름이 없거나 빈 문자열이면 코드만.

    5/14 사용자 피드백 — 텔레그램에서 종목 코드만 보이면 종목을 인지하기 어렵다.
    notify 콜 사이트가 `name=` 을 채워주면 빌더가 코드 옆에 합쳐 노출.
    """
    if isinstance(name, str) and name.strip():
        return f"{symbol} {name.strip()}"
    return symbol


def _format_symbol_list(raw: object, names: object) -> str:
    """`runner.started` 의 'symbols' (콤마/리스트) + symbol_names dict → '005930 삼성전자, …'.

    `symbols` 는 콤마 문자열(`"005930,403550"`) 또는 list 둘 다 지원. 비어있거나 None
    이면 '?' 로 폴백.
    """
    if isinstance(raw, list):
        items = [str(s) for s in raw if str(s)]
    elif isinstance(raw, str) and raw:
        items = [s for s in raw.split(",") if s]
    else:
        return "?"
    if not items:
        return "?"
    name_map: dict[str, str] = {}
    if isinstance(names, dict):
        for k, v in names.items():
            if isinstance(k, str) and isinstance(v, str):
                name_map[k] = v
    return ", ".join(_with_name(s, name_map.get(s)) for s in items)


def _format_message(event: str, fields: dict[str, Any]) -> str:
    """이벤트명 + kwargs 를 사람이 읽을 한 줄 메시지로."""
    if event == "runner.started":
        strategy = fields.get("strategy", "?")
        symbol_label = _format_symbol_list(fields.get("symbols"), fields.get("symbol_names"))
        dry = " (dry-run)" if fields.get("dry_run") else ""
        return f"▶️ paper 시작{dry}\n전략: {strategy}\n종목: {symbol_label}"
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
        sym_label = _with_name(str(fields.get("symbol", "?")), fields.get("name"))
        return (
            f"✅ 체결 접수 {sym_label} {str(fields.get('side', '?')).upper()}"
            f" {fields.get('qty', '?')}주"
            f" (ord:{fields.get('order_no', '-')})"
        )
    if event == "runner.order_failed":
        sym_label = _with_name(str(fields.get("symbol", "?")), fields.get("name"))
        return (
            f"❌ 주문 실패 {sym_label} {str(fields.get('side', '?')).upper()}"
            f" {fields.get('qty', '?')}주: {fields.get('error', 'unknown')}"
        )
    if event == "runner.balance_failed_fatal":
        return f"🛑 잔고 조회 {fields.get('consecutive', '?')}회 연속 실패 — 러너 중단"

    if event == "runner.kill_switch_triggered":
        # 23단계 Phase B — 일일 손실 한도 도달 시 강제 청산 + 정지 직전 푸시.
        limit = fields.get("daily_loss_limit")
        limit_part = f" (-{float(limit) * 100:.1f}%)" if limit is not None else ""
        return f"🛑 일일 손실 한도 도달{limit_part} — 강제 청산 후 정지"

    if event == "notify.throttled_summary":
        # 23단계 Phase H — ThrottledNotifier 가 N분 윈도우 모은 요약. 본문 그대로.
        return str(fields.get("text") or "🔁 (요약 비어있음)")

    if event == "runner.weekly_proposal":
        # 24단계 Phase D — auto-trade weekly-proposal --push. 본문 그대로.
        return str(fields.get("text") or "🗞 (제안 비어있음)")

    if event == "runner.holiday_skip":
        # 25단계 — KRX 휴장일 진입 가드. paper / paper-day / weekly-proposal 공용.
        cmd = fields.get("command", "?")
        d = fields.get("date", "?")
        next_d = fields.get("next_trading_day")
        next_part = f" · 다음 거래일 {next_d}" if next_d else ""
        return f"📅 KRX 휴장일({d}) — {cmd} 시동 생략{next_part}"

    if event == "position_guard.fired":
        # 23단계 Phase C — PositionGuard SELL 발화. stop/trailing/partial 한국어.
        sym_label = _with_name(str(fields.get("symbol", "?")), fields.get("name"))
        reason = str(fields.get("reason", ""))
        qty = fields.get("qty", "?")
        price = fields.get("price")
        dry = " (시뮬)" if fields.get("dry_run") else ""
        reason_ko = {
            "stop_loss": "🛟 손절",
            "trailing": "📉 트레일링",
            "partial_take": "💵 부분 익절",
        }.get(reason, "🛟 가드")
        price_part = f" @ {int(float(price)):,}원" if price is not None else ""
        return f"{reason_ko} {sym_label} {qty}주{price_part}{dry}"

    # ---- 21단계 day-trade 이벤트 ----------------------------------------

    if event == "daytrade.discovery_done":
        chosen = fields.get("chosen") or []
        blocked = fields.get("blocked") or []
        note = fields.get("note") or ""
        names_raw = fields.get("names") or {}
        name_map: dict[str, str] = {}
        if isinstance(names_raw, dict):
            for k, v in names_raw.items():
                if isinstance(k, str) and isinstance(v, str):
                    name_map[k] = v
        chosen_str = (
            ", ".join(_with_name(str(s), name_map.get(str(s))) for s in chosen)
            if chosen
            else "없음"
        )
        head = f"🔎 발굴 완료 · 매수 후보 {len(chosen)}건"
        body = f"\n선정: {chosen_str}"
        if blocked:
            blocked_summary = ", ".join(
                f"{_with_name(str(sym), name_map.get(str(sym)))}({reason})"
                for sym, reason in blocked[:5]
            )
            if len(blocked) > 5:
                blocked_summary += f" 외 {len(blocked) - 5}"
            body += f"\n차단: {blocked_summary}"
        if note:
            body += f"\n메모: {note}"
        return head + body

    if event == "runner.liquidate_done":
        # count = 성공 건수, failed = 실패 건수 (2026-05-08 분리). 둘 다 보여
        # 사용자가 "1종목 청산 완료" 푸시를 보고 실제 0건 청산을 오해하지 않게.
        success = fields.get("count", 0)
        failed = int(fields.get("failed") or 0)
        reason = fields.get("reason", "?")
        dry = " (dry-run)" if fields.get("dry_run") else ""
        if failed > 0 and success > 0:
            return f"⚠️ 청산 부분 성공{dry} · {success}종목 성공 / {failed}종목 실패 ({reason})"
        if failed > 0:
            return f"❌ 청산 전부 실패{dry} · {failed}종목 ({reason})"
        return f"🧹 청산 완료{dry} · {success}종목 ({reason})"

    if event == "runner.liquidate_failed":
        sym_label = _with_name(str(fields.get("symbol", "?")), fields.get("name"))
        return (
            f"❌ 청산 실패 {sym_label} {fields.get('qty', '?')}주: {fields.get('error', 'unknown')}"
        )

    # ---- 22단계 일일 리포트 ---------------------------------------------

    if event == "runner.daily_report":
        from .state import format_daily_report

        return format_daily_report(dict(fields))

    # ---- 22단계 주간 인사이트 (LLM) -------------------------------------

    if event == "runner.weekly_insight":
        from .insight import InsightResponse, format_insight_message

        try:
            findings_raw = fields.get("key_findings") or []
            recs_raw = fields.get("recommendations") or []
            response = InsightResponse(
                summary_ko=str(fields.get("summary_ko") or ""),
                key_findings=[str(x) for x in findings_raw]
                if isinstance(findings_raw, list)
                else [],
                recommendations=[str(x) for x in recs_raw] if isinstance(recs_raw, list) else [],
                confidence=float(fields.get("confidence") or 0.0),
            )
        except Exception:
            return f"🧭 주간 인사이트 (포맷 오류): {fields}"
        date_from = str(fields.get("date_from") or "")
        date_to = str(fields.get("date_to") or "")
        days = int(fields.get("days") or 0)
        date_range = (date_from, date_to) if date_from and date_to else None
        return format_insight_message(response, days=days, date_range=date_range)

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
        return f"TelegramNotifier(token={mask_secret(self._token)!r}, chat_id={self._chat_id!r})"

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
