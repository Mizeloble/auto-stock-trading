"""market.breadth 회귀 테스트.

JSONL 마지막 라운드 추출 + advancing/mean 계산 + 게이트 판정을
결정론적 fixture 로 잡는다.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from auto_trading.market import breadth as bd


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _round(ts: str, items: list[tuple[str, float]]) -> list[dict]:
    return [
        {
            "ts": ts,
            "symbol": sym,
            "change_pct": pct,
            "rank": i + 1,
            "name": f"종목{i}",
            "market": "KOSPI",
            "price": 100.0,
            "change": 0.0,
            "volume": 0,
        }
        for i, (sym, pct) in enumerate(items)
    ]


def test_compute_breadth_picks_only_latest_round(tmp_path: Path) -> None:
    """파일에 두 라운드(ts1, ts2)가 있어도 ts2 만 계산에 포함."""
    path = tmp_path / "topcap-2026-05-20.jsonl"
    rows = []
    rows += _round("2026-05-20T13:00:00+09:00", [("A", -5.0), ("B", -5.0), ("C", -5.0)])
    rows += _round("2026-05-20T13:10:00+09:00", [("A", 1.0), ("B", 2.0), ("C", -1.0), ("D", 0.5)])
    _write_jsonl(path, rows)

    b = bd.compute_breadth(tmp_path, when=date(2026, 5, 20))
    assert b.unavailable is False
    assert b.sample_size == 4
    assert b.advancing_pct == 0.75  # A,B,D 양수
    assert b.declining_pct == 0.25  # C 음수
    assert b.mean_change_pct == round((1.0 + 2.0 - 1.0 + 0.5) / 4, 4)
    assert b.ts == "2026-05-20T13:10:00+09:00"


def test_compute_breadth_unavailable_when_missing(tmp_path: Path) -> None:
    b = bd.compute_breadth(tmp_path, when=date(2026, 5, 20))
    assert b.unavailable is True
    assert b.sample_size == 0


def test_compute_breadth_skips_corrupt_lines(tmp_path: Path) -> None:
    path = tmp_path / "topcap-2026-05-20.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write('{"ts":"X","symbol":"A","change_pct":1.0}\n')
        fh.write("not json\n")
        fh.write('{"ts":"X","symbol":"B","change_pct":-2.0}\n')

    b = bd.compute_breadth(tmp_path, when=date(2026, 5, 20))
    assert b.sample_size == 2
    assert b.advancing_pct == 0.5


def test_gate_passes_when_both_thresholds_met() -> None:
    breadth = bd.Breadth(
        ts="X",
        sample_size=200,
        advancing_pct=0.65,
        declining_pct=0.30,
        mean_change_pct=0.4,
    )
    result = bd.evaluate_breadth_gate(breadth, min_advancing_pct=0.5, min_mean_change_pct=0.0)
    assert result.passed is True
    assert result.reason == "ok"


def test_gate_blocks_when_advancing_below() -> None:
    breadth = bd.Breadth(
        ts="X",
        sample_size=200,
        advancing_pct=0.30,
        declining_pct=0.65,
        mean_change_pct=0.4,  # mean 은 통과여도
    )
    result = bd.evaluate_breadth_gate(breadth, min_advancing_pct=0.5, min_mean_change_pct=0.0)
    assert result.passed is False
    assert result.reason == "advancing_pct_below"


def test_gate_blocks_when_mean_below() -> None:
    breadth = bd.Breadth(
        ts="X",
        sample_size=200,
        advancing_pct=0.55,
        declining_pct=0.40,
        mean_change_pct=-0.2,
    )
    result = bd.evaluate_breadth_gate(breadth, min_advancing_pct=0.5, min_mean_change_pct=0.0)
    assert result.passed is False
    assert result.reason == "mean_change_pct_below"


def test_gate_fail_open_when_unavailable() -> None:
    """데이터 부족이면 게이트 통과(기존 흐름 보존) + reason=no_data."""
    breadth = bd.Breadth.unavailable_instance("no_file")
    result = bd.evaluate_breadth_gate(breadth, min_advancing_pct=0.5, min_mean_change_pct=0.0)
    assert result.passed is True
    assert result.reason == "no_data"
