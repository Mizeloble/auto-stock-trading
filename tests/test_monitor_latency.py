"""monitor.latency 단위 회귀.

29단계 PR19 (#20).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from auto_trading._common import KST
from auto_trading.config import KiwoomMode, Settings
from auto_trading.monitor.latency import (
    LatencyTracker,
    _percentile,
    get_default_latency_tracker,
)


def test_percentile_empty_returns_zero() -> None:
    assert _percentile([], 50) == 0.0


def test_percentile_single_value() -> None:
    assert _percentile([42.0], 99) == 42.0


def test_percentile_p50_is_median() -> None:
    assert _percentile([1, 2, 3, 4, 5], 50) == 3.0


def test_percentile_p99_close_to_max() -> None:
    values = list(range(100))
    # p99 = rank 0.99 * 99 = 98.01 → 보간 = 98 + 0.01 * (99 - 98) ≈ 98.01
    assert 98 <= _percentile(values, 99) <= 99


def test_record_appends_jsonl(tmp_path: Path) -> None:
    tracker = LatencyTracker(path=tmp_path / "lat.jsonl")
    entry = tracker.record(label="order_send_ok", duration_ms=125.5)
    assert entry.duration_ms == 125.5
    assert entry.label == "order_send_ok"
    lines = (tmp_path / "lat.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert "order_send_ok" in lines[0]


def test_record_swallows_disk_failure(tmp_path: Path) -> None:
    tracker = LatencyTracker(path=tmp_path / "lat.jsonl")
    with patch.object(Path, "open", side_effect=OSError("disk full")):
        # 예외 X — 정상 entry 반환.
        entry = tracker.record(label="x", duration_ms=10)
    assert entry.duration_ms == 10


def test_replay_skips_corrupt_line(tmp_path: Path) -> None:
    path = tmp_path / "lat.jsonl"
    path.write_text(
        '{"ts":"2026-05-10T10:00:00+09:00","label":"a","duration_ms":10}\n'
        "이건 JSON 아님\n"
        '{"ts":"2026-05-10T11:00:00+09:00","label":"b","duration_ms":20}\n',
        encoding="utf-8",
    )
    tracker = LatencyTracker(path=path)
    entries = list(tracker.replay())
    assert len(entries) == 2
    assert {e.label for e in entries} == {"a", "b"}


def test_daily_stats_groups_by_label(tmp_path: Path) -> None:
    tracker = LatencyTracker(path=tmp_path / "lat.jsonl")
    ts = datetime(2026, 5, 10, 10, tzinfo=KST)
    for ms in [100, 200, 300, 400, 500]:
        tracker.record(label="order_send_ok", duration_ms=ms, ts=ts)
    tracker.record(label="order_send_fail", duration_ms=999, ts=ts)
    # 다른 날.
    tracker.record(
        label="order_send_ok",
        duration_ms=50,
        ts=ts + timedelta(days=1),
    )

    stats_list = tracker.daily_stats("2026-05-10")
    by_label = {s.label: s for s in stats_list}
    assert by_label["order_send_ok"].count == 5
    assert by_label["order_send_ok"].mean_ms == 300.0
    assert by_label["order_send_ok"].p50_ms == 300.0
    assert by_label["order_send_ok"].max_ms == 500.0
    assert by_label["order_send_fail"].count == 1
    assert by_label["order_send_fail"].p99_ms == 999.0


def test_daily_stats_empty_when_no_data(tmp_path: Path) -> None:
    tracker = LatencyTracker(path=tmp_path / "lat.jsonl")
    assert tracker.daily_stats("2026-05-10") == []


def test_get_default_tracker_returns_none_when_unset() -> None:
    s = Settings(
        kiwoom_mode=KiwoomMode.MOCK,
        kiwoom_app_key="k",
        kiwoom_app_secret="s",
        latency_log_path=None,
    )
    assert get_default_latency_tracker(s) is None


def test_get_default_tracker_returns_tracker_when_path_set(tmp_path: Path) -> None:
    path = tmp_path / "lat.jsonl"
    s = Settings(
        kiwoom_mode=KiwoomMode.MOCK,
        kiwoom_app_key="k",
        kiwoom_app_secret="s",
        latency_log_path=path,
    )
    tracker = get_default_latency_tracker(s)
    assert tracker is not None
    assert tracker.path == path
