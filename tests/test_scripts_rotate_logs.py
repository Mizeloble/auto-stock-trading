"""scripts/rotate-logs.sh 회귀.

검증:
- 7일 된 .log → 그대로 (gzip X)
- 35일 된 .log → .log.gz 됨
- 100일 된 .log.gz → 삭제됨
- 10MB+ launchd-*.log → truncate (size <= len(timestamp line))
- launchd-*.log 는 30일 넘어도 gzip 안 함 (제외 조건 회귀)
- LOGS_ROOT 가 없으면 조용히 exit 0
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "rotate-logs.sh"

# rotate-logs.sh 는 macOS BSD `stat -f%z` + zsh `(N)` 글롭 의존이라 Linux/no-zsh 환경에선
# 정상 동작 X. CI(우분투) 회피용 skip — 운영 타깃은 사용자 맥북 launchd.
pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("zsh") is None,
    reason="rotate-logs.sh requires macOS BSD stat + zsh (launchd target)",
)


def _set_mtime_days_ago(path: Path, days: int) -> None:
    """파일 mtime 을 N일 전으로 설정. find -mtime 매칭용."""
    target = time.time() - days * 86400
    os.utime(path, (target, target))


def _run_rotate(logs_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/zsh", str(SCRIPT)],
        env={**os.environ, "LOGS_ROOT": str(logs_root)},
        capture_output=True,
        text=True,
        check=False,
    )


def test_recent_log_kept_as_is(tmp_path: Path) -> None:
    """7일 된 .log 는 회전 대상 아님."""
    logs = tmp_path / "logs"
    logs.mkdir()
    f = logs / "paper-day-2026-05-01.log"
    f.write_text("recent")
    _set_mtime_days_ago(f, 7)

    result = _run_rotate(logs)
    assert result.returncode == 0, result.stderr
    assert f.exists()
    assert not (logs / "paper-day-2026-05-01.log.gz").exists()


def test_old_log_is_gzipped(tmp_path: Path) -> None:
    """35일 된 .log → .log.gz."""
    logs = tmp_path / "logs"
    logs.mkdir()
    f = logs / "paper-day-2026-04-01.log"
    f.write_text("old content here")
    _set_mtime_days_ago(f, 35)

    result = _run_rotate(logs)
    assert result.returncode == 0, result.stderr
    assert not f.exists(), "원본은 gzip 후 삭제돼야"
    gz = logs / "paper-day-2026-04-01.log.gz"
    assert gz.exists(), "gzip 결과물 없음"


def test_very_old_gz_is_deleted(tmp_path: Path) -> None:
    """100일 된 .log.gz 삭제."""
    logs = tmp_path / "logs"
    logs.mkdir()
    gz = logs / "paper-day-2026-01-01.log.gz"
    gz.write_bytes(b"\x1f\x8b\x08compressed-stub")
    _set_mtime_days_ago(gz, 100)

    result = _run_rotate(logs)
    assert result.returncode == 0, result.stderr
    assert not gz.exists()


def test_launchd_log_excluded_from_gzip(tmp_path: Path) -> None:
    """launchd-*.log 는 30일 넘어도 gzip X — launchd 가 같은 fd 유지."""
    logs = tmp_path / "logs"
    logs.mkdir()
    f = logs / "launchd-paper-day.out.log"
    f.write_text("small")
    _set_mtime_days_ago(f, 60)

    result = _run_rotate(logs)
    assert result.returncode == 0, result.stderr
    assert f.exists(), "launchd-*.log 가 gzip 됨 — append 끊김"
    assert not (logs / "launchd-paper-day.out.log.gz").exists()


def test_large_launchd_log_is_truncated(tmp_path: Path) -> None:
    """10MB 넘는 launchd-*.log 는 비우고 timestamp 1줄."""
    logs = tmp_path / "logs"
    logs.mkdir()
    f = logs / "launchd-paper-day.out.log"
    # 11MB 더미
    f.write_bytes(b"x" * (11 * 1024 * 1024))

    result = _run_rotate(logs)
    assert result.returncode == 0, result.stderr
    new_size = f.stat().st_size
    assert new_size < 200, f"truncate 후에도 큼: {new_size} bytes"
    # truncate timestamp line
    content = f.read_text()
    assert "truncated_at=" in content


def test_small_launchd_log_left_alone(tmp_path: Path) -> None:
    """10MB 미만 launchd-*.log 는 그대로."""
    logs = tmp_path / "logs"
    logs.mkdir()
    f = logs / "launchd-paper-day.out.log"
    f.write_bytes(b"y" * 1024)  # 1KB

    result = _run_rotate(logs)
    assert result.returncode == 0, result.stderr
    assert f.read_bytes() == b"y" * 1024


def test_missing_logs_root_exits_clean(tmp_path: Path) -> None:
    """LOGS_ROOT 가 없으면 silent exit 0."""
    nonexistent = tmp_path / "nope"
    result = _run_rotate(nonexistent)
    assert result.returncode == 0


def test_mixed_files_handled_in_one_pass(tmp_path: Path) -> None:
    """recent + old + ancient + launchd 가 섞여 있어도 정확히 분류."""
    logs = tmp_path / "logs"
    logs.mkdir()

    recent = logs / "paper-day-2026-05-08.log"
    recent.write_text("today")

    old_log = logs / "paper-day-2026-03-01.log"
    old_log.write_text("two months ago")
    _set_mtime_days_ago(old_log, 60)

    ancient_gz = logs / "paper-day-2025-12-01.log.gz"
    ancient_gz.write_bytes(b"\x1f\x8bcompressed")
    _set_mtime_days_ago(ancient_gz, 150)

    launchd = logs / "launchd-paper-day.out.log"
    launchd.write_text("small launchd content")
    _set_mtime_days_ago(launchd, 60)

    result = _run_rotate(logs)
    assert result.returncode == 0, result.stderr
    assert recent.exists()
    assert not old_log.exists()
    assert (logs / "paper-day-2026-03-01.log.gz").exists()
    assert not ancient_gz.exists()
    assert launchd.exists()  # 30일↑ 면역
