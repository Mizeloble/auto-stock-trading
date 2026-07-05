"""launchd LaunchAgent 상태 수집 (read-only).

macOS launchd 에 등록된 자동실행 작업(plist)의 마지막 실행 시각·exit code 를
모아 `LaunchdStatus` 리스트로 반환한다. 표 출력은 CLI(`auto-trade ops`) 담당.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .._common import KST

# 1회성 발화 후 자기 정리가 안 된 라벨. 표에 "별도 정리" 비고로만 표시 — 실제
# launchctl bootout + plist 삭제는 별도 작업.
_EXPIRED_ONESHOT_LABELS = frozenset({"com.mizeloble.auto-trade.churnfix-verify"})

# launchctl print 출력의 "last exit code" 값을 그대로 캡쳐. 정수("0", "1")는 물론
# "(never exited)" 같은 비정수 sentinel 도 표시 단계에서 구분하기 위해 raw string.
_LAST_EXIT_RE = re.compile(r"last exit code\s*=\s*(\S.*?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class LaunchdStatus:
    """LaunchAgent 한 건의 운영 상태 스냅샷."""

    label: str
    last_run: datetime | None
    exit_status: str | None
    note: str


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


def _fallback_log_mtime(label: str, logs_dir: Path) -> datetime | None:
    """`logs/launchd-<short>.{out,err}.log` 중 newer mtime 을 마지막 실행 시각으로."""
    short = label.removeprefix("com.mizeloble.auto-trade.")
    candidates = [
        logs_dir / f"launchd-{short}.out.log",
        logs_dir / f"launchd-{short}.err.log",
    ]
    mtimes = [c.stat().st_mtime for c in candidates if c.exists()]
    if not mtimes:
        return None
    return datetime.fromtimestamp(max(mtimes), tz=KST)


def collect_launchd_status(launchd_dir: Path, logs_dir: Path) -> list[LaunchdStatus]:
    """plist 폴더 + 로그 폴더에서 모든 LaunchAgent 의 상태 수집."""
    rows: list[LaunchdStatus] = []
    for label in _scan_launchd_labels(launchd_dir):
        rows.append(
            LaunchdStatus(
                label=label,
                last_run=_fallback_log_mtime(label, logs_dir),
                exit_status=_launchctl_last_exit_status(label),
                note="⚠ 만료된 1회성, 별도 정리" if label in _EXPIRED_ONESHOT_LABELS else "",
            )
        )
    return rows
