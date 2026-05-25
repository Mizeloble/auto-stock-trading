"""monitor.price_snapshot 회귀 테스트.

monitor/latency.py 테스트 패턴을 따라 — 가짜 client + 가짜 get_quote 로
JSONL append, best-effort 디스크 오류 swallow, 한 종목 실패 격리를 잡는다.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from auto_trading._common import KST
from auto_trading.market.marketcap import MarketCapEntry
from auto_trading.market.quote import Quote
from auto_trading.monitor import price_snapshot as ps


def _make_universe(n: int = 3) -> list[MarketCapEntry]:
    syms = [
        ("005930", "삼성전자", "KOSPI"),
        ("000660", "SK하이닉스", "KOSPI"),
        ("247540", "에코프로비엠", "KOSDAQ"),
    ]
    return [
        MarketCapEntry(
            rank=i + 1,
            symbol=sym,
            name=name,
            market=market,
            shares=1_000_000,
            close=10_000,
            market_cap=10_000_000_000 - i,
        )
        for i, (sym, name, market) in enumerate(syms[:n])
    ]


async def test_record_round_appends_jsonl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    quotes = {
        "005930": Quote(
            symbol="005930",
            price=Decimal("70000"),
            change=Decimal("1000"),
            change_pct=Decimal("1.45"),
            volume=10_000_000,
        ),
        "000660": Quote(
            symbol="000660",
            price=Decimal("150000"),
            change=Decimal("-2000"),
            change_pct=Decimal("-1.31"),
            volume=5_000_000,
        ),
        "247540": Quote(
            symbol="247540",
            price=Decimal("200000"),
            change=Decimal("0"),
            change_pct=Decimal("0"),
            volume=2_000_000,
        ),
    }

    async def fake_get_quote(_client, sym):
        return quotes[sym]

    monkeypatch.setattr(ps, "get_quote", fake_get_quote)

    recorder = ps.PriceSnapshotRecorder(path=tmp_path / "topcap.jsonl")
    now = datetime(2026, 5, 20, 13, 10, tzinfo=KST)
    entries = await recorder.record_round(client=None, universe=_make_universe(3), now=now)

    assert len(entries) == 3
    lines = (tmp_path / "topcap.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3

    first = json.loads(lines[0])
    assert first["symbol"] == "005930"
    assert first["price"] == 70000.0
    assert first["change_pct"] == 1.45
    assert first["rank"] == 1
    # 라운드 안에서 ts 가 같아야 (시계열 정렬 단위)
    timestamps = {json.loads(line)["ts"] for line in lines}
    assert len(timestamps) == 1
    assert "2026-05-20T13:10:00" in next(iter(timestamps))


async def test_record_round_isolates_single_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """한 종목 호출 실패는 다른 종목 기록을 막지 않는다."""

    async def fake_get_quote(_client, sym):
        if sym == "000660":
            raise RuntimeError("키움 일시 오류")
        return Quote(
            symbol=sym, price=Decimal("1"), change=Decimal("0"), change_pct=Decimal("0"), volume=1
        )

    monkeypatch.setattr(ps, "get_quote", fake_get_quote)

    recorder = ps.PriceSnapshotRecorder(path=tmp_path / "topcap.jsonl")
    entries = await recorder.record_round(client=None, universe=_make_universe(3))

    assert len(entries) == 2
    symbols = [e.symbol for e in entries]
    assert "000660" not in symbols


async def test_record_round_swallows_disk_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_get_quote(_client, sym):
        return Quote(
            symbol=sym, price=Decimal("1"), change=Decimal("0"), change_pct=Decimal("0"), volume=1
        )

    monkeypatch.setattr(ps, "get_quote", fake_get_quote)
    recorder = ps.PriceSnapshotRecorder(path=tmp_path / "topcap.jsonl")

    with patch.object(Path, "open", side_effect=OSError("disk full")):
        # 예외 X — 호출 모두 시도 후 정상 종료(append 만 실패).
        entries = await recorder.record_round(client=None, universe=_make_universe(2))

    assert len(entries) == 2  # 호출은 성공, 디스크 쓰기만 swallow


def test_snapshot_path_for_uses_date(tmp_path: Path) -> None:
    from datetime import date as _date

    p = ps.snapshot_path_for(tmp_path, when=_date(2026, 5, 20))
    assert p == tmp_path / "topcap-2026-05-20.jsonl"


def test_index_snapshot_path_for_uses_date(tmp_path: Path) -> None:
    from datetime import date as _date

    p = ps.index_snapshot_path_for(tmp_path, when=_date(2026, 5, 20))
    assert p == tmp_path / "indices-2026-05-20.jsonl"


async def test_index_recorder_appends_two_indices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = {"KOSPI": 2730.12, "KOSDAQ": 850.45}

    async def fake_fetch(_client, market):
        return values[market]

    monkeypatch.setattr(ps, "fetch_latest_index_price", fake_fetch)

    recorder = ps.IndexSnapshotRecorder(path=tmp_path / "indices.jsonl")
    now = datetime(2026, 5, 20, 13, 10, tzinfo=KST)
    entries = await recorder.record_round(client=None, now=now)

    assert len(entries) == 2
    lines = (tmp_path / "indices.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2

    payloads = [json.loads(line) for line in lines]
    by_market = {p["market"]: p for p in payloads}
    assert by_market["KOSPI"]["value"] == 2730.12
    assert by_market["KOSDAQ"]["value"] == 850.45
    # 라운드 안에서 ts 동일
    timestamps = {p["ts"] for p in payloads}
    assert len(timestamps) == 1


async def test_index_recorder_skips_none_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fetch_latest_index_price 가 None 반환하면 그 지수는 라인 안 남김."""

    async def fake_fetch(_client, market):
        return 2730.12 if market == "KOSPI" else None

    monkeypatch.setattr(ps, "fetch_latest_index_price", fake_fetch)

    recorder = ps.IndexSnapshotRecorder(path=tmp_path / "indices.jsonl")
    entries = await recorder.record_round(client=None)

    assert len(entries) == 1
    assert entries[0].market == "KOSPI"
    lines = (tmp_path / "indices.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


async def test_index_recorder_isolates_single_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_fetch(_client, market):
        if market == "KOSPI":
            raise RuntimeError("키움 일시 오류")
        return 850.45

    monkeypatch.setattr(ps, "fetch_latest_index_price", fake_fetch)

    recorder = ps.IndexSnapshotRecorder(path=tmp_path / "indices.jsonl")
    entries = await recorder.record_round(client=None)

    assert len(entries) == 1
    assert entries[0].market == "KOSDAQ"
