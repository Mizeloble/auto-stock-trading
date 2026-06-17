"""account.untradeable 회귀 — 거래불가 이력 영속·노화.

2026-06-16: 모의투자 매매제한 종목이 슬롯을 낭비하던 문제 대응. 거부된 종목을
디스크에 누적해 다음 거래일 발굴이 사전 회피하되, within_days 안에 다시 안
걸리면 자동 만료.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from auto_trading.account.untradeable import UntradeableStore


def _store(tmp_path: Path) -> UntradeableStore:
    return UntradeableStore(path=tmp_path / "untradeable.json")


def test_record_and_active_symbols(tmp_path: Path) -> None:
    store = _store(tmp_path)
    today = date(2026, 6, 16)
    store.record("004870", today=today, name="티웨이홀딩스", reason="trading_halt")

    assert store.active_symbols(today) == {"004870"}
    entry = store.load()["004870"]
    assert entry.name == "티웨이홀딩스"
    assert entry.reason == "trading_halt"
    assert entry.first_date == "2026-06-16"
    assert entry.last_date == "2026-06-16"
    assert entry.count == 1


def test_same_day_repeat_does_not_inflate_count(tmp_path: Path) -> None:
    """하루 62번 거부돼도 count 는 1 (일수 기준)."""
    store = _store(tmp_path)
    today = date(2026, 6, 16)
    for _ in range(62):
        store.record("004870", today=today, reason="trading_halt")
    assert store.load()["004870"].count == 1


def test_repeat_on_new_day_increments_count(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record("004870", today=date(2026, 6, 16), reason="trading_halt")
    store.record("004870", today=date(2026, 6, 17), reason="trading_halt")
    entry = store.load()["004870"]
    assert entry.count == 2
    assert entry.first_date == "2026-06-16"
    assert entry.last_date == "2026-06-17"


def test_active_symbols_ages_out_old_entries(tmp_path: Path) -> None:
    """within_days 를 넘긴 종목은 발굴 제외 목록에서 빠진다(자동 만료)."""
    store = _store(tmp_path)
    store.record("004870", today=date(2026, 5, 1), reason="trading_halt")
    today = date(2026, 6, 16)  # 46일 경과
    assert store.active_symbols(today, within_days=30) == set()
    assert store.active_symbols(today, within_days=60) == {"004870"}


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    assert _store(tmp_path).load() == {}
    assert _store(tmp_path).active_symbols(date(2026, 6, 16)) == set()


def test_record_empty_symbol_is_noop(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record("", today=date(2026, 6, 16))
    assert store.load() == {}


def test_corrupt_file_does_not_raise(tmp_path: Path) -> None:
    path = tmp_path / "untradeable.json"
    path.write_text("{ not json", encoding="utf-8")
    store = UntradeableStore(path=path)
    assert store.load() == {}  # 손상 → 빈 dict, 예외 없음
