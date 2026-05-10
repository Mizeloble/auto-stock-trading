"""risk.consecutive_loss 회귀.

29단계 PR9 (#14 1차).

검증:
- threshold=0 → 모든 호출 no-op (비활성).
- 손절 streak 누적 + threshold 도달 시 paused_until 설정.
- 양수 PnL 한 번만 와도 streak reset (이후 손절은 다시 1부터).
- paused_until 만료 시 자동 청소 (is_paused 호출 시).
- 수동 unpause 가 streak 도 reset.
- paused 상태에서 새 손절은 streak 갱신은 하지만 paused_until 은 *연장* (재발 방어).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from auto_trading._common import KST
from auto_trading.risk.consecutive_loss import ConsecutiveLossTracker


def _now() -> datetime:
    return datetime(2026, 5, 10, 10, 0, 0, tzinfo=KST)


def test_disabled_when_threshold_zero() -> None:
    tracker = ConsecutiveLossTracker(threshold=0)
    for _ in range(10):
        tracker.record_round_trip(-1000)
    assert tracker.streak == 0
    assert tracker.is_paused() is False


def test_streak_increments_on_loss() -> None:
    tracker = ConsecutiveLossTracker(threshold=3)
    tracker.record_round_trip(-100, now=_now())
    tracker.record_round_trip(-200, now=_now())
    assert tracker.streak == 2
    assert tracker.is_paused(now=_now()) is False  # 아직 임계 미달


def test_pause_triggered_at_threshold() -> None:
    tracker = ConsecutiveLossTracker(threshold=3, pause_seconds=3600)
    now = _now()
    tracker.record_round_trip(-100, now=now)
    tracker.record_round_trip(-100, now=now)
    tracker.record_round_trip(-100, now=now)  # 3번째 → paused

    assert tracker.streak == 3
    assert tracker.paused_until == now + timedelta(seconds=3600)
    assert tracker.is_paused(now=now) is True


def test_positive_pnl_resets_streak_but_not_paused() -> None:
    """양수 PnL 은 streak 만 reset. 이미 paused 면 paused_until 유지."""
    tracker = ConsecutiveLossTracker(threshold=2, pause_seconds=3600)
    now = _now()
    tracker.record_round_trip(-100, now=now)
    tracker.record_round_trip(-100, now=now)  # paused
    assert tracker.is_paused(now=now)
    assert tracker.streak == 2

    tracker.record_round_trip(+200, now=now)
    assert tracker.streak == 0
    # paused 는 만료까지 유지 — 자동 회복은 시간 흐름으로만.
    assert tracker.is_paused(now=now) is True


def test_pause_expires_naturally() -> None:
    tracker = ConsecutiveLossTracker(threshold=2, pause_seconds=60)
    now = _now()
    tracker.record_round_trip(-100, now=now)
    tracker.record_round_trip(-100, now=now)
    assert tracker.is_paused(now=now) is True

    after = now + timedelta(seconds=61)
    assert tracker.is_paused(now=after) is False
    assert tracker.paused_until is None  # 청소됨
    assert tracker.streak == 0  # streak 도 reset


def test_manual_unpause_clears_state() -> None:
    tracker = ConsecutiveLossTracker(threshold=2)
    now = _now()
    tracker.record_round_trip(-100, now=now)
    tracker.record_round_trip(-100, now=now)
    assert tracker.is_paused(now=now)

    tracker.unpause()
    assert tracker.is_paused(now=now) is False
    assert tracker.streak == 0
    assert tracker.paused_until is None


def test_loss_during_pause_extends_window() -> None:
    """이미 paused 인 상태에서 또 손절 — paused_until 갱신(연장)."""
    tracker = ConsecutiveLossTracker(threshold=2, pause_seconds=3600)
    now = _now()
    tracker.record_round_trip(-100, now=now)
    tracker.record_round_trip(-100, now=now)
    first_until = tracker.paused_until

    later = now + timedelta(seconds=1800)
    tracker.record_round_trip(-100, now=later)  # streak=3, threshold 이미 넘었음

    assert tracker.streak == 3
    assert tracker.paused_until == later + timedelta(seconds=3600)
    assert tracker.paused_until > first_until  # type: ignore[operator]


def test_zero_pnl_treated_as_breakeven_resets_streak() -> None:
    """pnl == 0 은 손절 아님 — streak reset."""
    tracker = ConsecutiveLossTracker(threshold=3)
    tracker.record_round_trip(-100)
    tracker.record_round_trip(-100)
    assert tracker.streak == 2
    tracker.record_round_trip(0.0)
    assert tracker.streak == 0
