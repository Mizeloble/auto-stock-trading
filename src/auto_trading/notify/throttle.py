"""ThrottledNotifier — N분 윈도우 내 매매 이벤트를 1개 요약 메시지로.

23단계 Phase H. day-trade 가 다회 진입 + stop/trailing/partial_take 로 매매 빈도가
높아지면 텔레그램이 채터링으로 도배되는 문제를 막는다.

- **즉시 통과**: 시작/종료/kill switch/잔고 조회 실패/주간 인사이트/일일 리포트 등
  중요 이벤트는 throttle 우회.
- **요약 대상**: order_accepted / order_failed / liquidate_done / liquidate_failed /
  position_guard.fired / daytrade.discovery_done — 이들은 buffer 에 한 줄씩 모아
  윈도우 경과 시 또는 aclose() 시 inner 에 `notify.throttled_summary` 이벤트로 1회.

윈도우 트리거는 timer task 가 아니라 매 notify 시점에 "지나간 윈도우면 flush" —
별도 task 의존이 없어 단순하고 시뮬·테스트 용이.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import structlog

from .base import Notifier

log = structlog.get_logger(__name__)

#: 즉시 푸시 (throttle 무시) 이벤트. 그 외는 모두 buffer.
_PASSTHROUGH: frozenset[str] = frozenset(
    {
        "runner.started",
        "runner.stopped",
        "runner.balance_failed_fatal",
        "runner.kill_switch_triggered",
        "runner.kill_switch_blind",
        "runner.weekly_insight",
        "runner.daily_report",
    }
)


def _won_amount(fields: dict) -> str:
    """총 거래대금(수량 곱하기 단가) ' (3,395,000원)'. 가격/수량 없거나 0 이면 빈 문자열.

    2026-06-15 사용자 요청 — 체결 요약이 '몇 주'만 보여줘 투입/회수 금액을 알기 어려웠다.
    요약은 압축이 목적이라 단가(@)는 생략하고 총액만 괄호로 덧붙인다.
    """
    price = fields.get("price")
    if price is None:
        return ""
    try:
        unit = float(price)
        shares = int(fields.get("qty"))  # type: ignore[arg-type]
    except TypeError, ValueError:
        return ""
    total = unit * shares
    if total <= 0:
        return ""
    return f" ({round(total):,}원)"


#: 요약 대상 이벤트와 사람이 읽는 한국어 짧은 라벨.
_LABELS: dict[str, Callable[[dict], str]] = {
    "runner.order_accepted": (
        lambda f: (
            f"✅ {f.get('symbol', '?')} {str(f.get('side', '?')).upper()} "
            f"{f.get('qty', '?')}주{_won_amount(f)}"
        )
    ),
    "runner.order_failed": (
        lambda f: (
            f"❌ {f.get('symbol', '?')} {str(f.get('side', '?')).upper()} {f.get('qty', '?')}주 실패"
        )
    ),
    "runner.liquidate_done": (
        lambda f: f"🧹 청산 {f.get('count', 0)}종목" + (" (시뮬)" if f.get("dry_run") else "")
    ),
    "runner.liquidate_failed": (
        lambda f: f"❌ 청산 실패 {f.get('symbol', '?')} {f.get('qty', '?')}주"
    ),
    "position_guard.fired": (
        lambda f: (
            f"{_GUARD_EMOJI.get(str(f.get('reason', '')), '🛟')} "
            f"{f.get('symbol', '?')} {f.get('qty', '?')}주"
        )
    ),
    "daytrade.discovery_done": (lambda f: f"🔎 발굴 {len(f.get('chosen') or [])}건"),
}

_GUARD_EMOJI: dict[str, str] = {
    "stop_loss": "🛟 손절",
    "trailing": "📉 트레일",
    "partial_take": "💵 익절",
}

#: 분당 요약 이벤트가 이 값을 넘으면 과매매 의심 경고를 머리에 붙인다.
#: 손실 핵심 원인이 장중 과회전이라 사용자가 빈도를 바로 인지하게 한다.
_OVERTRADE_RATE_PER_MIN = 2.0


@dataclass
class ThrottledNotifier:
    """다른 Notifier 를 wrap 해 요약 윈도우 적용.

    `inner` 가 NoopNotifier / TelegramNotifier 둘 중 무엇이든 동작. 윈도우 = `window_min` 분.
    """

    inner: Notifier
    window_min: int = 5
    _now: Callable[[], datetime] = field(default=datetime.now)
    _buffer: list[str] = field(default_factory=list)
    _window_start: datetime | None = field(default=None)

    async def notify(self, event: str, **fields: object) -> None:
        if event in _PASSTHROUGH or event not in _LABELS:
            await self.inner.notify(event, **fields)
            return
        await self._maybe_flush()
        if self._window_start is None:
            self._window_start = self._now()
        try:
            line = _LABELS[event](fields)
        except Exception:
            line = f"{event}: {fields}"
        self._buffer.append(line)

    async def aclose(self) -> None:
        await self._flush()
        await self.inner.aclose()

    async def _maybe_flush(self) -> None:
        if self._window_start is None:
            return
        if self._now() - self._window_start >= timedelta(minutes=self.window_min):
            await self._flush()

    async def _flush(self) -> None:
        if not self._buffer:
            self._window_start = None
            return
        n = len(self._buffer)
        rate = n / self.window_min if self.window_min else float(n)
        header = f"🔁 최근 {self.window_min}분 요약 ({n}건 · 약 {rate:.1f}건/분)"
        if rate > _OVERTRADE_RATE_PER_MIN:
            header = "⚠️ 과매매 의심 — 회전 과다\n" + header
        summary = header + "\n" + "\n".join(self._buffer)
        try:
            await self.inner.notify("notify.throttled_summary", text=summary)
        except Exception:
            log.warning("notify.throttled_flush_failed", count=len(self._buffer), exc_info=True)
        self._buffer.clear()
        self._window_start = None


__all__ = ["ThrottledNotifier"]
