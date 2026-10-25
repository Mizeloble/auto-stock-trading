"""장중 벽시계 공백 감지 — 맥북 슬립으로 러너가 통째로 멈췄다 깨어난 구간을 잡는다.

무엇/왜:
- 2026-10-06~16 Amphetamine 세션이 꺼진 채 맥북이 ~17분 주기 DarkWake 로만 깨어,
  지수 모멘텀 기록 간격이 정상 1분 → 중앙 16.5분으로 늘었다. heartbeat-check(launchd)
  도 같이 잠들어 기존 감시로는 아무 경보가 없었다.
- 슬립 중엔 프로세스 자체가 멈추므로 "멈춘 동안" 알릴 방법은 없다. 대신 깨어난 직후
  자기 루프의 직전 체크 시각과 현재 벽시계 시각 차이로 공백을 사후 감지한다.
  (asyncio.sleep·monotonic 은 macOS 슬립 동안 흐르지 않을 수 있어 벽시계로 잰다.)

어떻게 확인: tests/test_runner_wallclock.py — 시각을 주입해 공백을 시뮬레이션.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time

from .._common import KST


def _now_kst() -> datetime:
    return datetime.now(KST)


@dataclass(frozen=True)
class WallclockGap:
    """감지된 공백 1건. `notify` 는 오늘 첫 공백일 때만 True (텔레그램 하루 1회)."""

    since: datetime
    until: datetime
    gap_min: float
    notify: bool


@dataclass
class WallclockGapDetector:
    """직전 관측 시각 대비 현재 시각 차가 임계를 넘으면 `WallclockGap` 을 돌려준다.

    - 장중(`market_open`~`market_close`, KST) 에 깨어난 공백만 센다 — 장 밖 공백은
      수집 손실이 아니므로 무시.
    - 로그는 공백마다 남기되(호출자 책임), 텔레그램은 하루 1회만 — DarkWake 처럼 공백이
      하루 수십 번 반복돼도 알림 도배가 되지 않게. 운영 paper-day 는 ThrottledNotifier
      를 켜지 않으므로 이 하루 1회 제한을 감지기 자체가 쥔다.
    """

    threshold_min: float = 5.0
    check_interval_sec: float = 30.0
    market_open: time = time(9, 0)
    market_close: time = time(15, 20)
    clock: Callable[[], datetime] = field(default=_now_kst)
    _last: datetime | None = field(default=None, init=False)
    _notified_on: date | None = field(default=None, init=False)

    def observe(self, now: datetime | None = None) -> WallclockGap | None:
        """현재 시각을 기록하고, 직전 관측과의 공백이 임계 초과면 반환."""
        now = now if now is not None else self.clock()
        last, self._last = self._last, now
        if last is None:
            return None
        gap_min = (now - last).total_seconds() / 60.0
        if gap_min < self.threshold_min:
            return None
        local = now.astimezone(KST)
        if not (self.market_open <= local.time() <= self.market_close):
            return None
        notify = self._notified_on != local.date()
        if notify:
            self._notified_on = local.date()
        return WallclockGap(since=last, until=now, gap_min=gap_min, notify=notify)


__all__ = ["WallclockGap", "WallclockGapDetector"]
