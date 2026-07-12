"""launchd LaunchAgent 상태 수집 (read-only).

macOS launchd 에 등록된 자동실행 작업(plist)의 마지막 실행 시각·exit code·
다음 예정 시각·최근 7일 실행일 통계를 모아 `LaunchdStatus` 리스트로 반환한다.
표 출력은 CLI(`auto-trade ops`) 담당.

로그 원천 2종 (2026-07-12 수정 — "마지막 실행 5월 고착" 버그):
- 레거시: plist `StandardOutPath` 의 ``logs/launchd-<short>.{out,err}.log``.
- 날짜별: 래퍼 스크립트가 직접 쓰는 ``logs/<short>-YYYY-MM-DD.log``
  (insight-weekly 만 예외적으로 ``insight-`` 접두사). 대부분 잡이 이쪽만 갱신하므로
  두 원천 중 최신 mtime 을 마지막 실행으로 삼는다.
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from xml.parsers.expat import ExpatError

from .._common import KST

# 1회성 발화 후 자기 정리가 안 된 라벨. 표에 "별도 정리" 비고로만 표시 — 실제
# launchctl bootout + plist 삭제는 별도 작업.
_EXPIRED_ONESHOT_LABELS = frozenset({"com.mizeloble.auto-trade.churnfix-verify"})

# launchctl print 출력의 "last exit code" 값을 그대로 캡쳐. 정수("0", "1")는 물론
# "(never exited)" 같은 비정수 sentinel 도 표시 단계에서 구분하기 위해 raw string.
_LAST_EXIT_RE = re.compile(r"last exit code\s*=\s*(\S.*?)\s*$", re.MULTILINE)

# 날짜별 로그 접두사가 라벨 short 와 다른 잡. 나머지는 short 그대로.
_DATED_LOG_PREFIX = {"insight-weekly": "insight"}

# 다음 예정 탐색 상한 — churnfix-verify 처럼 연 1회(Month+Day 고정) 스케줄도 잡는다.
_NEXT_FIRE_SCAN_DAYS = 371


@dataclass(frozen=True)
class LaunchdStatus:
    """LaunchAgent 한 건의 운영 상태 스냅샷."""

    label: str
    last_run: datetime | None
    exit_status: str | None
    note: str
    next_run: datetime | None = None
    interval_sec: int | None = None
    expected_days_7d: int | None = None
    actual_days_7d: int | None = None


@dataclass(frozen=True)
class _Schedule:
    """plist 의 스케줄 키 — 캘린더 항목들 또는 고정 간격(초)."""

    calendar: tuple[dict[str, int], ...] = ()
    interval_sec: int | None = None


def _scan_launchd_labels(launchd_dir: Path) -> list[str]:
    """`com.mizeloble.auto-trade.*.plist` 파일명에서 라벨 목록 도출."""
    return [p.stem for p in sorted(launchd_dir.glob("com.mizeloble.auto-trade.*.plist"))]


def _launchctl_last_exit_status(label: str) -> str | None:
    """`launchctl print gui/$UID/<label>` 출력의 `last exit code = X` 에서 X 그대로 반환.

    예) "0" (정상), "1" (오류), "(never exited)" (부팅 후 미실행).
    macOS launchctl 미존재(다른 OS·테스트 환경)·subprocess 실패·grep miss 시 None.
    호출자가 None / "0" / "(never exited)" / 기타 정수 네 가지 케이스를 표시 단계에서 분기.
    """
    try:
        result = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError, subprocess.SubprocessError:
        return None
    if result.returncode != 0:
        return None
    m = _LAST_EXIT_RE.search(result.stdout)
    return m.group(1).strip() if m else None


def _dated_logs(short: str, logs_dir: Path) -> list[tuple[date, Path]]:
    """래퍼 스크립트가 쓰는 날짜별 로그 ``<prefix>-YYYY-MM-DD.log`` 목록 (파싱 실패 스킵)."""
    prefix = _DATED_LOG_PREFIX.get(short, short)
    pattern = re.compile(rf"^{re.escape(prefix)}-(\d{{4}}-\d{{2}}-\d{{2}})\.log$")
    out: list[tuple[date, Path]] = []
    for p in logs_dir.glob(f"{prefix}-*.log"):
        m = pattern.match(p.name)
        if m is None:
            continue
        try:
            out.append((date.fromisoformat(m.group(1)), p))
        except ValueError:
            continue
    return out


def _fallback_log_mtime(label: str, logs_dir: Path) -> datetime | None:
    """레거시 launchd-*.log + 날짜별 로그 전체에서 최신 mtime 을 마지막 실행 시각으로."""
    short = label.removeprefix("com.mizeloble.auto-trade.")
    candidates = [
        logs_dir / f"launchd-{short}.out.log",
        logs_dir / f"launchd-{short}.err.log",
    ]
    candidates.extend(p for _, p in _dated_logs(short, logs_dir))
    mtimes = [c.stat().st_mtime for c in candidates if c.exists()]
    if not mtimes:
        return None
    return datetime.fromtimestamp(max(mtimes), tz=KST)


def _load_schedule(plist_path: Path) -> _Schedule | None:
    """plist 의 StartCalendarInterval / StartInterval 파싱. 실패·스케줄 없음이면 None."""
    try:
        with plist_path.open("rb") as fh:
            data: dict[str, Any] = plistlib.load(fh)
    except OSError, plistlib.InvalidFileException, ExpatError, ValueError, TypeError:
        return None

    interval = data.get("StartInterval")
    if isinstance(interval, int) and interval > 0:
        return _Schedule(interval_sec=interval)

    raw = data.get("StartCalendarInterval")
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return None
    entries: list[dict[str, int]] = []
    for item in raw:
        if isinstance(item, dict):
            entries.append({k: v for k, v in item.items() if isinstance(v, int)})
    if not entries:
        return None
    return _Schedule(calendar=tuple(entries))


def _date_matches(entry: dict[str, int], d: date) -> bool:
    """캘린더 항목의 날짜 조건(Month/Day/Weekday) 검사 — 없는 키는 wildcard.

    Weekday 는 launchd semantics (0·7=일요일) ↔ Python isoweekday(월=1…일=7) 변환.
    """
    if "Month" in entry and entry["Month"] != d.month:
        return False
    if "Day" in entry and entry["Day"] != d.day:
        return False
    return "Weekday" not in entry or entry["Weekday"] % 7 == d.isoweekday() % 7


def _fires_on(entry: dict[str, int], d: date) -> list[datetime]:
    """캘린더 항목이 날짜 d 에 발화하는 시각들. Hour/Minute 없으면 wildcard."""
    if not _date_matches(entry, d):
        return []
    hours = [entry["Hour"]] if "Hour" in entry else list(range(24))
    minutes = [entry["Minute"]] if "Minute" in entry else list(range(60))
    return [
        datetime(d.year, d.month, d.day, h, m, tzinfo=KST)
        for h in hours
        if 0 <= h <= 23
        for m in minutes
        if 0 <= m <= 59
    ]


def _next_fire(calendar: tuple[dict[str, int], ...], now: datetime) -> datetime | None:
    """now 이후 첫 발화 시각. 최대 1년+1주 탐색 (연 1회 스케줄 커버)."""
    for day_offset in range(_NEXT_FIRE_SCAN_DAYS):
        d = (now + timedelta(days=day_offset)).date()
        fires = [t for e in calendar for t in _fires_on(e, d) if t > now]
        if fires:
            return min(fires)
    return None


def _expected_days_7d(schedule: _Schedule, now: datetime) -> int:
    """최근 7일 중 스케줄상 1회 이상 발화 예정이었던 날 수 (오늘 제외, 어제까지).

    오늘을 빼는 이유: 오늘 예정분은 아직 안 돌았을 수 있어 '미실행'과 구분 불가.
    """
    if schedule.interval_sec is not None:
        return 7
    count = 0
    for day_offset in range(1, 8):
        d = (now - timedelta(days=day_offset)).date()
        if any(_date_matches(e, d) for e in schedule.calendar):
            count += 1
    return count


def _actual_days_7d(short: str, logs_dir: Path, now: datetime) -> int | None:
    """최근 7일(오늘 제외) 중 날짜별 로그가 생성된 날 수.

    날짜별 로그를 아예 안 쓰는 잡(heartbeat-check·log-rotate 등)은 측정 불가 → None.
    """
    dated = _dated_logs(short, logs_dir)
    if not dated:
        return None
    window_start = (now - timedelta(days=7)).date()
    today = now.date()
    return len({d for d, _ in dated if window_start <= d < today})


def collect_launchd_status(
    launchd_dir: Path,
    logs_dir: Path,
    now: datetime | None = None,
) -> list[LaunchdStatus]:
    """plist 폴더 + 로그 폴더에서 모든 LaunchAgent 의 상태 수집."""
    if now is None:
        now = datetime.now(KST)
    rows: list[LaunchdStatus] = []
    for label in _scan_launchd_labels(launchd_dir):
        short = label.removeprefix("com.mizeloble.auto-trade.")
        schedule = _load_schedule(launchd_dir / f"{label}.plist")
        next_run: datetime | None = None
        interval_sec: int | None = None
        expected: int | None = None
        if schedule is not None:
            interval_sec = schedule.interval_sec
            if schedule.calendar:
                next_run = _next_fire(schedule.calendar, now)
            expected = _expected_days_7d(schedule, now)
        rows.append(
            LaunchdStatus(
                label=label,
                last_run=_fallback_log_mtime(label, logs_dir),
                exit_status=_launchctl_last_exit_status(label),
                note="⚠ 만료된 1회성, 별도 정리" if label in _EXPIRED_ONESHOT_LABELS else "",
                next_run=next_run,
                interval_sec=interval_sec,
                expected_days_7d=expected,
                actual_days_7d=_actual_days_7d(short, logs_dir, now),
            )
        )
    return rows
