"""봇 heartbeat / dead-man switch.

29단계 PR3 (#13) — 봇이 조용히 멈췄을 때(WebSocket 끊김 + 재연결 실패,
런타임 데드락 등) 사용자가 모르는 채 시간이 흐르는 위험을 막는다.

구조:
- ``Heartbeat`` (이 모듈) — 러너가 일정 간격으로 ``write()`` 를 호출해서
  파일 mtime 을 갱신. 단순한 atomic write.
- ``auto-trade heartbeat-check`` (CLI) — 별도 cron(launchd) 이 5분마다 실행.
  파일 mtime 이 ``max_age_sec`` 를 초과하거나 파일이 없으면 텔레그램에 경고.

설계 원칙:
- 외부 API 의존 0. 디스크 mtime 만 사용 — Python 파싱 오류 가능성 차단.
- write 실패는 swallow + 구조화 로그. 디스크 가득 / 권한 문제로 봇 자체가
  멈추지 않게.
- cooldown 으로 spam 방지: cron 이 5분마다 돌아도 같은 누락 상황에서
  텔레그램은 ``cooldown_sec`` 1회.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import structlog

from ._common import KST

log = structlog.get_logger(__name__)

__all__ = [
    "DEFAULT_COOLDOWN_PATH",
    "DEFAULT_COOLDOWN_SEC",
    "DEFAULT_HEARTBEAT_PATH",
    "DEFAULT_INTERVAL_SEC",
    "DEFAULT_MAX_AGE_SEC",
    "DEFAULT_STOPPED_PATH",
    "Heartbeat",
    "HeartbeatStatus",
    "check_heartbeat",
]


DEFAULT_HEARTBEAT_PATH = Path("data/heartbeat/runner.txt")
DEFAULT_COOLDOWN_PATH = Path("data/heartbeat/last_alert.txt")
# 29단계 회귀(2026-05-11) — 러너가 정상 종료한 직후에도 cron 이 mtime 만 보고
# "heartbeat missing" 알림을 30분마다 쏘는 문제 차단용. 러너 finally 블록이
# 이 sentinel 을 touch 하고, check_heartbeat 는 sentinel 이 heartbeat 보다
# 최근이면 ok=True (reason="stopped") 로 처리한다.
DEFAULT_STOPPED_PATH = Path("data/heartbeat/stopped.txt")
DEFAULT_INTERVAL_SEC = 1800  # 30분 — 러너가 갱신
DEFAULT_MAX_AGE_SEC = 3600  # 1시간 — cron 이 누락으로 판정
DEFAULT_COOLDOWN_SEC = 1800  # 30분 — 같은 누락에 알림 1회


@dataclass
class Heartbeat:
    """러너용 heartbeat 갱신기."""

    path: Path = DEFAULT_HEARTBEAT_PATH
    interval_sec: int = DEFAULT_INTERVAL_SEC
    # 29단계 회귀(2026-05-11) — 정상 종료 sentinel. 러너 finally 블록이
    # mark_stopped() 호출, 시작 시 clear_stopped() 호출. cron 은
    # check_heartbeat 가 ok="stopped" 로 응답하면 알림 생략.
    stopped_path: Path = DEFAULT_STOPPED_PATH

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stopped_path.parent.mkdir(parents=True, exist_ok=True)

    def write(self) -> None:
        """현재 시각(ISO 8601 KST)을 파일에 truncate-write.

        파일 mtime 이 외부 모니터의 진단 기준. 파일 안의 timestamp 는 사람이
        직접 cat 해서 확인할 때 도움이 되는 보조 정보일 뿐.
        """
        try:
            self.path.write_text(
                datetime.now(tz=KST).isoformat() + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            log.error("heartbeat.write_failed", path=str(self.path), error=str(exc))

    def mark_stopped(self) -> None:
        """정상 종료 sentinel touch. 디스크 실패는 swallow + 로그."""
        try:
            self.stopped_path.write_text(
                datetime.now(tz=KST).isoformat() + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            log.error("heartbeat.stopped_write_failed", path=str(self.stopped_path), error=str(exc))

    def clear_stopped(self) -> None:
        """다음 사이클 시작 — sentinel 정리. 없어도 무시."""
        try:
            self.stopped_path.unlink(missing_ok=True)
        except OSError as exc:
            log.error("heartbeat.stopped_clear_failed", path=str(self.stopped_path), error=str(exc))


@dataclass(frozen=True)
class HeartbeatStatus:
    """check_heartbeat 결과. CLI 와 단위 테스트가 공용."""

    ok: bool
    reason: str  # "ok" / "missing" / "stale" / "stopped"
    age_sec: float | None  # 정상/stale 일 때 파일 나이. missing/stopped 이면 None.


def check_heartbeat(
    path: Path,
    max_age_sec: int,
    *,
    now: float | None = None,
    stopped_path: Path | None = None,
) -> HeartbeatStatus:
    """heartbeat 파일 상태 진단. 외부 호출 없음 — 순수 함수처럼 동작.

    `now` 는 단위 테스트에서 시간을 고정하기 위한 주입. None 이면 wall-clock.

    29단계 회귀(2026-05-11) — `stopped_path` 가 존재하고 heartbeat 보다 최근이면
    "정상 종료" 로 판정해 ok=True, reason="stopped" 반환. 러너가 finally 블록에서
    mark_stopped() 한 다음에는 cron 이 missing/stale 알림을 30분마다 쏘지 않게.
    """
    current = time.time() if now is None else now
    stopped = stopped_path if stopped_path is not None else DEFAULT_STOPPED_PATH

    hb_mtime = path.stat().st_mtime if path.exists() else None
    if stopped.exists():
        stopped_mtime = stopped.stat().st_mtime
        # heartbeat 가 stopped 보다 더 최근이면 sentinel 은 stale — 무시 (러너 재시동 후
        # clear 가 누락된 보호용). 그 외에는 ok="stopped".
        if hb_mtime is None or stopped_mtime >= hb_mtime:
            return HeartbeatStatus(ok=True, reason="stopped", age_sec=None)

    if hb_mtime is None:
        return HeartbeatStatus(ok=False, reason="missing", age_sec=None)
    age = current - hb_mtime
    if age > max_age_sec:
        return HeartbeatStatus(ok=False, reason="stale", age_sec=age)
    return HeartbeatStatus(ok=True, reason="ok", age_sec=age)
