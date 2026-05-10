"""auto_trading.heartbeat 회귀.

29단계 PR3 (#13).

검증:
- Heartbeat.__post_init__ 가 부모 디렉터리를 생성한다.
- write() 가 파일에 ISO8601 timestamp 한 줄을 truncate-write 한다.
- write() 의 디스크 실패는 swallow + 로그 (예외 전파 X).
- check_heartbeat:
    - 파일 없음 → ok=False, reason="missing", age_sec=None
    - 파일 있고 mtime 신선 → ok=True, reason="ok"
    - 파일 있고 mtime 오래됨 → ok=False, reason="stale"
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from auto_trading.heartbeat import Heartbeat, check_heartbeat


def test_init_creates_parent_directory(tmp_path: Path) -> None:
    nested = tmp_path / "deeply" / "nested" / "runner.txt"
    Heartbeat(path=nested)
    assert nested.parent.is_dir()


def test_write_writes_iso_timestamp(tmp_path: Path) -> None:
    hb = Heartbeat(path=tmp_path / "runner.txt")
    hb.write()
    text = hb.path.read_text(encoding="utf-8").strip()
    # KST offset (+09:00) 포함 ISO 8601.
    assert "T" in text
    assert text.endswith("+09:00")


def test_write_truncates_previous_content(tmp_path: Path) -> None:
    """매 호출이 덮어쓰기 — 파일이 무한히 자라지 않게."""
    hb = Heartbeat(path=tmp_path / "runner.txt")
    hb.path.write_text("이전 내용 매우 김\n" * 100, encoding="utf-8")
    hb.write()
    assert len(hb.path.read_text(encoding="utf-8").splitlines()) == 1


def test_write_swallows_oserror(tmp_path: Path) -> None:
    """디스크 실패가 봇 자체를 멈추지 않게 swallow + 로그."""
    hb = Heartbeat(path=tmp_path / "runner.txt")
    with patch.object(Path, "write_text", side_effect=OSError("disk full")):
        hb.write()  # 예외 X


def test_check_heartbeat_missing_file(tmp_path: Path) -> None:
    status = check_heartbeat(tmp_path / "nope.txt", max_age_sec=3600)
    assert status.ok is False
    assert status.reason == "missing"
    assert status.age_sec is None


def test_check_heartbeat_fresh_file(tmp_path: Path) -> None:
    path = tmp_path / "runner.txt"
    path.write_text("now\n", encoding="utf-8")
    # 가짜 시각으로 file mtime 직후 1초 지난 시점을 모사.
    fake_now = path.stat().st_mtime + 1
    status = check_heartbeat(path, max_age_sec=3600, now=fake_now)
    assert status.ok is True
    assert status.reason == "ok"
    assert status.age_sec is not None
    assert 0 <= status.age_sec <= 2


def test_check_heartbeat_stale_file(tmp_path: Path) -> None:
    path = tmp_path / "runner.txt"
    path.write_text("old\n", encoding="utf-8")
    # mtime 을 의도적으로 옛날로 옮긴다.
    old_mtime = path.stat().st_mtime - 7200  # 2시간 전
    os.utime(path, (old_mtime, old_mtime))
    status = check_heartbeat(path, max_age_sec=3600)
    assert status.ok is False
    assert status.reason == "stale"
    assert status.age_sec is not None
    assert status.age_sec >= 3600
