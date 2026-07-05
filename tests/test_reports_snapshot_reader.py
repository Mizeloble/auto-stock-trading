"""topcap 일중 스냅샷 리더 — 시계열 조회·조인 헬퍼."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from auto_trading.reports.snapshot_reader import (
    parse_topcap_for_date,
    parse_topcap_snapshots,
)


def _line(ts: str, symbol: str, price: float, change_pct: float, rank: int) -> str:
    return json.dumps(
        {
            "ts": ts,
            "rank": rank,
            "symbol": symbol,
            "name": symbol,
            "market": "KOSPI",
            "price": price,
            "change": 0.0,
            "change_pct": change_pct,
            "volume": 100,
        }
    )


def _write(tmp_path: Path, date: str, lines: list[str]) -> Path:
    p = tmp_path / f"topcap-{date}.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_parse_builds_symbol_series_sorted_by_minute(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "2026-06-15",
        [
            _line("2026-06-15T10:00:00+09:00", "A", 110.0, 10.0, 3),
            _line("2026-06-15T09:00:00+09:00", "A", 100.0, 0.0, 5),  # 더 이른 시각
        ],
    )
    snaps = parse_topcap_for_date("2026-06-15", root=tmp_path)
    pts = snaps.series["A"]
    assert [p.minute for p in pts] == [540, 600]  # 09:00, 10:00 정렬됨


def test_change_pct_and_rank_at_nearest_point(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "2026-06-15",
        [
            _line("2026-06-15T09:00:00+09:00", "A", 100.0, 1.0, 5),
            _line("2026-06-15T10:00:00+09:00", "A", 120.0, 20.0, 2),
        ],
    )
    snaps = parse_topcap_for_date("2026-06-15", root=tmp_path)
    # 09:05 는 09:00 에 더 가까움
    assert snaps.change_pct_at("A", "09:05") == 1.0
    assert snaps.rank_at("A", "09:05") == 5
    # 09:50 는 10:00 에 더 가까움
    assert snaps.change_pct_at("A", "09:50") == 20.0
    assert snaps.rank_at("A", "09:50") == 2


def test_range_during_returns_low_high_within_window(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "2026-06-15",
        [
            _line("2026-06-15T09:00:00+09:00", "A", 100.0, 0.0, 5),
            _line("2026-06-15T09:30:00+09:00", "A", 130.0, 30.0, 5),  # 구간 내 최고
            _line("2026-06-15T10:00:00+09:00", "A", 90.0, -10.0, 5),  # 구간 내 최저
            _line("2026-06-15T11:00:00+09:00", "A", 200.0, 100.0, 5),  # 구간 밖
        ],
    )
    snaps = parse_topcap_for_date("2026-06-15", root=tmp_path)
    assert snaps.range_during("A", "09:00", "10:00") == (90.0, 130.0)
    # 구간 내 포인트 없으면 None
    assert snaps.range_during("A", "12:00", "13:00") is None


def test_day_outcome_intraday_and_day_change(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "2026-06-15",
        [
            _line("2026-06-15T09:00:00+09:00", "A", 100.0, 1.0, 5),
            _line("2026-06-15T15:00:00+09:00", "A", 110.0, 11.0, 5),
        ],
    )
    snaps = parse_topcap_for_date("2026-06-15", root=tmp_path)
    intraday, day_change = snaps.day_outcome("A")
    assert intraday == pytest.approx(10.0)  # 100 → 110
    assert day_change == 11.0  # 마지막 포인트 등락률


def test_missing_file_and_corrupt_lines_are_safe(tmp_path: Path) -> None:
    # 없는 파일 → 빈 리더, 모든 조회 None
    empty = parse_topcap_for_date("2099-01-01", root=tmp_path)
    assert empty.change_pct_at("A", "09:00") is None
    assert empty.day_outcome("A") == (None, None)
    # 손상 라인·필드 결손은 skip
    p = tmp_path / "topcap-2026-06-15.jsonl"
    p.write_text(
        "\n".join(
            [
                "not json at all",
                "{bad json",
                json.dumps({"symbol": "A", "ts": "bad-ts", "price": 100.0}),  # ts 파싱 실패
                json.dumps({"ts": "2026-06-15T09:00:00+09:00", "price": 100.0}),  # symbol 없음
                _line("2026-06-15T09:00:00+09:00", "B", 100.0, 5.0, 1),  # 유효
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    snaps = parse_topcap_snapshots(p)
    assert "A" not in snaps.series
    assert snaps.change_pct_at("B", "09:00") == 5.0


def test_parse_for_date_merges_discovered_file(tmp_path: Path) -> None:
    """topcap + discovered 두 파일이 하나의 리더로 병합된다 (2026-07-04 커버리지 확장).

    발굴 소형주(시총 200 밖)는 discovered-<date>.jsonl 에만 있고, 병합 후에는
    topcap 종목과 동일하게 진입 등락률·MFE/MAE 조인이 가능해야 한다.
    """
    from auto_trading.reports.snapshot_reader import parse_topcap_for_date

    date = "2026-07-06"
    _write(tmp_path, date, [_line(f"{date}T09:10:00+09:00", "005930", 340000.0, 1.5, 1)])
    discovered = tmp_path / f"discovered-{date}.jsonl"
    discovered.write_text(
        _line(f"{date}T09:10:00+09:00", "001210", 1472.0, 25.0, 0)
        + "\n"
        + _line(f"{date}T09:20:00+09:00", "001210", 1400.0, 19.0, 0)
        + "\n",
        encoding="utf-8",
    )

    snap = parse_topcap_for_date(date, root=tmp_path)

    # topcap 종목은 기존대로, 발굴 소형주도 같은 API 로 조회 가능.
    assert snap.change_pct_at("005930", "09:10") == 1.5
    assert snap.change_pct_at("001210", "09:12") == 25.0
    assert snap.range_during("001210", "09:10", "09:20") == (1400.0, 1472.0)


def test_parse_for_date_without_discovered_file_unchanged(tmp_path: Path) -> None:
    """discovered 파일이 없는 과거 일자는 topcap 만으로 기존과 동일하게 동작."""
    from auto_trading.reports.snapshot_reader import parse_topcap_for_date

    date = "2026-06-15"
    _write(tmp_path, date, [_line(f"{date}T09:10:00+09:00", "005930", 340000.0, 1.5, 1)])

    snap = parse_topcap_for_date(date, root=tmp_path)

    assert snap.change_pct_at("005930", "09:10") == 1.5
    assert snap.change_pct_at("001210", "09:10") is None
