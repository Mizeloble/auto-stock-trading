"""장중 벽시계 공백(맥북 슬립) 감지 — 시각 주입으로 공백을 시뮬레이션.

2026-10-06~16 Amphetamine 꺼짐 사고 회귀: 러너가 ~17분 주기로만 깨어났는데 아무 경보가
없었다. 깨어난 직후 공백을 잡아 로그 + 텔레그램(하루 1회)이 나가는지 확인한다.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from auto_trading._common import KST
from auto_trading.notify.telegram import _format_message
from auto_trading.notify.throttle import _PASSTHROUGH
from auto_trading.runner import PaperRunner, RunnerControlState
from auto_trading.runner.wallclock import WallclockGapDetector
from auto_trading.strategy.sma_crossover import SmaCrossover


def _kst(hh: int, mm: int, day: int = 13) -> datetime:
    return datetime(2026, 10, day, hh, mm, tzinfo=KST)


# --- 감지기(순수 로직) ---------------------------------------------------------


def test_first_observation_never_reports() -> None:
    d = WallclockGapDetector()
    assert d.observe(_kst(10, 0)) is None


def test_normal_interval_is_not_a_gap() -> None:
    d = WallclockGapDetector(threshold_min=5.0)
    d.observe(_kst(10, 0))
    assert d.observe(_kst(10, 0) + timedelta(seconds=30)) is None
    assert d.observe(_kst(10, 4)) is None  # 4분 < 5분


def test_sleep_gap_over_threshold_is_reported_with_minutes() -> None:
    """실측 패턴 — 중앙 16.5분 공백."""
    d = WallclockGapDetector(threshold_min=5.0)
    d.observe(_kst(10, 12))
    gap = d.observe(_kst(10, 12) + timedelta(minutes=16, seconds=30))
    assert gap is not None
    assert gap.gap_min == 16.5
    assert gap.since == _kst(10, 12)
    assert gap.notify is True


def test_notify_only_once_per_day_but_every_gap_reported() -> None:
    """DarkWake 처럼 하루 수십 번 반복돼도 텔레그램은 1회, 감지(로그용)는 매번."""
    d = WallclockGapDetector()
    d.observe(_kst(9, 30))
    first = d.observe(_kst(9, 47))
    second = d.observe(_kst(10, 4))
    assert first is not None and first.notify is True
    assert second is not None and second.notify is False


def test_next_day_notifies_again() -> None:
    d = WallclockGapDetector()
    d.observe(_kst(9, 30, day=13))
    assert d.observe(_kst(9, 47, day=13)).notify is True  # type: ignore[union-attr]
    assert d.observe(_kst(10, 4, day=13)).notify is False  # type: ignore[union-attr]
    # 날짜가 바뀌면 다시 알림 기회 (하루 1회 = KST 달력 기준).
    gap = d.observe(_kst(9, 47, day=14))
    assert gap is not None and gap.notify is True


def test_gap_outside_market_hours_is_ignored() -> None:
    """장 밖에서 깨어난 공백은 수집 손실이 아니므로 무시 — 알림 기회도 소모하지 않는다."""
    d = WallclockGapDetector()
    d.observe(_kst(15, 25))
    assert d.observe(_kst(15, 50)) is None
    d.observe(_kst(8, 30))
    assert d.observe(_kst(8, 50)) is None
    # 장 시작 전 잠들어 장중에 깨어난 경우는 장중 수집 손실 — 잡는다.
    gap = d.observe(_kst(9, 20))
    assert gap is not None and gap.notify is True


def test_utc_clock_is_judged_in_kst() -> None:
    """UTC 로 들어와도 KST 장중 여부로 판정 (01:00Z = 10:00 KST)."""
    from datetime import UTC

    d = WallclockGapDetector()
    d.observe(datetime(2026, 10, 13, 1, 0, tzinfo=UTC))
    gap = d.observe(datetime(2026, 10, 13, 1, 17, tzinfo=UTC))
    assert gap is not None and gap.notify is True


# --- 러너 background loop ----------------------------------------------------


@dataclass
class _RecordingNotifier:
    calls: list[tuple[str, dict]] = field(default_factory=list)

    async def notify(self, event: str, **fields: object) -> None:
        self.calls.append((event, dict(fields)))

    async def aclose(self) -> None:
        return None


def _scripted_clock(times: list[datetime], runner: PaperRunner) -> object:
    """정해진 시각을 차례로 돌려주고, 다 쓰면 러너를 STOPPING 으로 — 루프 종료."""
    it: Iterator[datetime] = iter(times)
    last = times[-1]

    def clock() -> datetime:
        try:
            return next(it)
        except StopIteration:
            runner._control_state = RunnerControlState.STOPPING
            return last

    return clock


async def test_loop_logs_each_gap_and_notifies_once() -> None:
    notifier = _RecordingNotifier()
    runner = PaperRunner(strategy=SmaCrossover(), symbols=["005930"], notifier=notifier)
    times = [
        _kst(10, 0),
        _kst(10, 0) + timedelta(seconds=30),  # 정상
        _kst(10, 17),  # 슬립 공백 1 → 알림
        _kst(10, 34),  # 슬립 공백 2 → 로그만
    ]
    detector = WallclockGapDetector(check_interval_sec=0)
    detector.clock = _scripted_clock(times, runner)  # type: ignore[assignment]

    await asyncio.wait_for(runner._wallclock_gap_loop(detector), timeout=1.0)

    assert [e for e, _ in notifier.calls] == ["runner.wallclock_gap"]
    fields = notifier.calls[0][1]
    assert fields["since"] == "10:00"
    assert fields["until"] == "10:17"
    assert fields["gap_min"] == 16.5


async def test_loop_survives_notifier_failure() -> None:
    """알림 실패가 감시 루프(=러너)를 멈추면 안 된다."""

    class _Boom:
        async def notify(self, event: str, **fields: object) -> None:
            raise RuntimeError("telegram down")

        async def aclose(self) -> None:
            return None

    runner = PaperRunner(strategy=SmaCrossover(), symbols=["005930"], notifier=_Boom())
    detector = WallclockGapDetector(check_interval_sec=0)
    detector.clock = _scripted_clock([_kst(10, 0), _kst(10, 20)], runner)  # type: ignore[assignment]
    await asyncio.wait_for(runner._wallclock_gap_loop(detector), timeout=1.0)


async def test_run_starts_and_cancels_gap_task(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """run() 은 세션 본문(universe 대기 포함) 내내 감시 task 를 돌리고 끝나면 정리한다."""
    runner = PaperRunner(strategy=SmaCrossover(), symbols=["005930"])
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def fake_loop(detector: WallclockGapDetector) -> None:
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def fake_session() -> None:
        await asyncio.wait_for(started.wait(), timeout=1.0)

    monkeypatch.setattr(runner, "_wallclock_gap_loop", fake_loop)
    monkeypatch.setattr(runner, "_run_session", fake_session)
    await runner.run()
    assert cancelled.is_set()


async def test_run_without_detector_skips_task(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    runner = PaperRunner(strategy=SmaCrossover(), symbols=["005930"], wallclock_gap=None)
    calls: list[str] = []

    async def fake_session() -> None:
        calls.append("session")

    monkeypatch.setattr(runner, "_run_session", fake_session)
    await runner.run()
    assert calls == ["session"]


# --- notify 템플릿·분류 --------------------------------------------------------


def test_wallclock_gap_message_is_beginner_friendly() -> None:
    text = _format_message(
        "runner.wallclock_gap", {"gap_min": 16.5, "since": "10:12", "until": "10:28"}
    )
    assert "runner.wallclock_gap" not in text
    assert "gap_min" not in text
    assert "약 16분" in text or "약 17분" in text
    assert "10:12→10:28" in text
    assert "Amphetamine" in text
    assert "하루 한 번" in text


def test_wallclock_gap_message_tolerates_missing_fields() -> None:
    text = _format_message("runner.wallclock_gap", {})
    assert "몇 분" in text
    assert "None" not in text


def test_wallclock_gap_is_passthrough_in_throttle() -> None:
    assert "runner.wallclock_gap" in _PASSTHROUGH
