"""ThrottledNotifier — N분 윈도우 매매 이벤트 요약 단위 테스트."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from auto_trading.notify.throttle import ThrottledNotifier


@dataclass
class _RecordingNotifier:
    """inner Notifier 가짜 — 호출 기록만 보관."""

    calls: list[tuple[str, dict]] = field(default_factory=list)
    closed: bool = False

    async def notify(self, event: str, **fields: object) -> None:
        self.calls.append((event, dict(fields)))

    async def aclose(self) -> None:
        self.closed = True


class _Clock:
    def __init__(self, base: datetime) -> None:
        self._now = base

    def __call__(self) -> datetime:
        return self._now

    def advance(self, minutes: int = 0, seconds: int = 0) -> None:
        self._now += timedelta(minutes=minutes, seconds=seconds)


def _throttled(window_min: int = 5):  # type: ignore[no-untyped-def]
    inner = _RecordingNotifier()
    clock = _Clock(datetime(2026, 5, 7, 9, 0))
    notifier = ThrottledNotifier(inner=inner, window_min=window_min, _now=clock)
    return notifier, inner, clock


async def test_passthrough_events_send_immediately() -> None:
    """runner.started/stopped/kill_switch 등은 즉시 통과."""
    n, inner, _ = _throttled()
    await n.notify("runner.started", strategy="x", symbols="AAA", dry_run=True)
    await n.notify("runner.kill_switch_triggered", daily_loss_limit=0.03)
    await n.notify("runner.stopped", reason="ok")
    assert [c[0] for c in inner.calls] == [
        "runner.started",
        "runner.kill_switch_triggered",
        "runner.stopped",
    ]


async def test_throttle_buffers_events_until_window_elapses() -> None:
    """5분 안 매매 이벤트는 buffer 에만 누적, 즉시 push X."""
    n, inner, clock = _throttled(window_min=5)
    await n.notify("runner.order_accepted", symbol="A", side="buy", qty=10)
    await n.notify("position_guard.fired", symbol="A", reason="stop_loss", qty=10)
    assert inner.calls == []  # 아직 windows 안 지남

    clock.advance(minutes=5, seconds=1)
    # 다음 이벤트 들어오는 시점에 maybe_flush 가 동작 → 요약 1건 + 새 이벤트 buffer.
    await n.notify("runner.order_failed", symbol="B", side="buy", qty=5)
    assert len(inner.calls) == 1
    event, payload = inner.calls[0]
    assert event == "notify.throttled_summary"
    text = str(payload["text"])
    assert "5분 요약" in text
    assert "A BUY 10주" in text
    assert "🛟 손절" in text  # position_guard stop_loss 라벨


async def test_aclose_flushes_remaining_buffer() -> None:
    """aclose() 가 buffer 를 마지막으로 비운다."""
    n, inner, _ = _throttled()
    await n.notify("runner.order_accepted", symbol="A", side="buy", qty=10)
    await n.notify("runner.order_accepted", symbol="B", side="buy", qty=5)
    await n.aclose()
    assert inner.closed is True
    summaries = [c for c in inner.calls if c[0] == "notify.throttled_summary"]
    assert len(summaries) == 1
    text = str(summaries[0][1]["text"])
    assert "A BUY 10주" in text
    assert "B BUY 5주" in text


async def test_summary_includes_total_amount_when_price_present() -> None:
    """2026-06-15 요청 — 체결 요약 줄에 총 거래대금(수량 곱하기 단가)이 붙는다."""
    n, inner, _ = _throttled()
    await n.notify("runner.order_accepted", symbol="A", side="buy", qty=10, price=339500.0)
    await n.aclose()
    text = str(next(c for c in inner.calls if c[0] == "notify.throttled_summary")[1]["text"])
    assert "A BUY 10주 (3,395,000원)" in text


async def test_summary_omits_amount_without_price() -> None:
    """price 미기록이면 금액 없이 기존 압축 형식 유지 — 회귀 보호."""
    n, inner, _ = _throttled()
    await n.notify("runner.order_accepted", symbol="A", side="buy", qty=10)
    await n.aclose()
    text = str(next(c for c in inner.calls if c[0] == "notify.throttled_summary")[1]["text"])
    assert "A BUY 10주" in text
    assert "원)" not in text


async def test_summary_header_shows_count_and_rate() -> None:
    """요약 머리에 건수·분당 빈도 노출, 저빈도면 경고 없음."""
    n, inner, _ = _throttled(window_min=5)
    await n.notify("runner.order_accepted", symbol="A", side="buy", qty=10)
    await n.notify("runner.order_accepted", symbol="B", side="buy", qty=5)
    await n.aclose()
    text = str(next(c for c in inner.calls if c[0] == "notify.throttled_summary")[1]["text"])
    assert "(2건 · 약 0.4건/분)" in text
    assert "과매매 의심" not in text


async def test_summary_warns_on_overtrading_rate() -> None:
    """분당 2건 초과면 과매매 의심 경고를 머리에 붙인다."""
    n, inner, _ = _throttled(window_min=5)
    for i in range(11):  # 11건 / 5분 = 2.2건/분 > 2.0
        await n.notify("runner.order_accepted", symbol=f"S{i}", side="buy", qty=1)
    await n.aclose()
    text = str(next(c for c in inner.calls if c[0] == "notify.throttled_summary")[1]["text"])
    assert "⚠️ 과매매 의심" in text
    assert "(11건 · 약 2.2건/분)" in text


async def test_aclose_with_empty_buffer_is_noop() -> None:
    """buffer 비어있으면 throttled_summary 푸시 X."""
    n, inner, _ = _throttled()
    await n.aclose()
    assert all(c[0] != "notify.throttled_summary" for c in inner.calls)


async def test_unknown_event_passes_through() -> None:
    """label 매핑 없는 이벤트는 즉시 통과 — buffer 안 들어감."""
    n, inner, _ = _throttled()
    await n.notify("daytrade.universe_chosen", symbols=["A"])
    assert inner.calls == [("daytrade.universe_chosen", {"symbols": ["A"]})]


async def test_window_resets_after_flush() -> None:
    """flush 후 새 이벤트는 새 윈도우 시작."""
    n, inner, clock = _throttled(window_min=5)
    await n.notify("runner.order_accepted", symbol="A", side="buy", qty=10)
    clock.advance(minutes=5, seconds=10)
    await n.notify("runner.order_accepted", symbol="B", side="buy", qty=5)
    # 이 시점에 1차 요약 flush 됨, B 는 새 buffer.
    assert sum(1 for c in inner.calls if c[0] == "notify.throttled_summary") == 1
    # 추가 5분 안 — B 만 있는 버퍼는 아직 push X.
    clock.advance(minutes=2)
    await n.aclose()
    summaries = [c for c in inner.calls if c[0] == "notify.throttled_summary"]
    assert len(summaries) == 2
    assert "A BUY 10주" in str(summaries[0][1]["text"])
    assert "B BUY 5주" in str(summaries[1][1]["text"])


async def test_throttled_label_partial_take_emoji() -> None:
    """position_guard partial_take 는 💵 익절 라벨로 표시."""
    n, inner, _ = _throttled()
    await n.notify(
        "position_guard.fired",
        symbol="018880",
        reason="partial_take",
        qty=5,
        price=10200.0,
        dry_run=False,
    )
    await n.aclose()
    summary = next(c for c in inner.calls if c[0] == "notify.throttled_summary")
    text = str(summary[1]["text"])
    assert "💵 익절" in text
    assert "018880" in text
