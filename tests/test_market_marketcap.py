"""market.marketcap 회귀 테스트.

pykrx 호출은 monkeypatch 로 가짜 데이터 주입 — 외부 네트워크 호출 없이
정렬·top_n·캐시 라운드트립을 잡는다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from auto_trading.market import marketcap as mc


def _fake_fetch(kospi: list[tuple], kosdaq: list[tuple]):
    """`_fetch_market` 를 대체할 가짜 함수 — 시장별 (symbol,name,cap,shares,close) 리스트."""

    def fn(query_date: date, market: mc.Market):
        if market == "KOSPI":
            return list(kospi)
        if market == "KOSDAQ":
            return list(kosdaq)
        return []

    return fn


def test_compute_merges_and_sorts_by_marketcap_desc(monkeypatch: pytest.MonkeyPatch) -> None:
    kospi = [
        ("005930", "삼성전자", 500_000_000_000_000, 5_969_000_000, 84_000),
        ("000660", "SK하이닉스", 100_000_000_000_000, 728_000_000, 137_000),
    ]
    kosdaq = [
        ("247540", "에코프로비엠", 30_000_000_000_000, 97_000_000, 309_000),
        ("086520", "에코프로", 20_000_000_000_000, 26_000_000, 769_000),
    ]
    monkeypatch.setattr(mc, "_fetch_market", _fake_fetch(kospi, kosdaq))

    entries = mc.compute_top_marketcap_universe(top_n=4, base_date=date(2026, 5, 20))

    assert [e.symbol for e in entries] == ["005930", "000660", "247540", "086520"]
    assert entries[0].rank == 1
    assert entries[0].name == "삼성전자"
    assert entries[0].market == "KOSPI"
    assert entries[2].market == "KOSDAQ"
    # market_cap desc 정렬 보장
    caps = [e.market_cap for e in entries]
    assert caps == sorted(caps, reverse=True)


def test_compute_top_n_truncates(monkeypatch: pytest.MonkeyPatch) -> None:
    kospi = [
        (f"00000{i}", f"종목{i}", (10 - i) * 1_000_000_000, 1000, 100)
        for i in range(10)
    ]
    monkeypatch.setattr(mc, "_fetch_market", _fake_fetch(kospi, []))

    entries = mc.compute_top_marketcap_universe(top_n=3, base_date=date(2026, 5, 20))
    assert len(entries) == 3
    # rank 1,2,3
    assert [e.rank for e in entries] == [1, 2, 3]


def test_compute_skips_failed_market(monkeypatch: pytest.MonkeyPatch) -> None:
    """KOSPI 가 깨져도 KOSDAQ 결과만으로 진행되어야 한다."""

    def fn(query_date: date, market: mc.Market):
        if market == "KOSPI":
            raise RuntimeError("KRX 일시 장애")
        return [("247540", "에코프로비엠", 30_000_000_000_000, 97_000_000, 309_000)]

    monkeypatch.setattr(mc, "_fetch_market", fn)
    entries = mc.compute_top_marketcap_universe(top_n=5, base_date=date(2026, 5, 20))
    assert len(entries) == 1
    assert entries[0].symbol == "247540"


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    entries = [
        mc.MarketCapEntry(
            rank=1,
            symbol="005930",
            name="삼성전자",
            market="KOSPI",
            shares=5_969_000_000,
            close=84_000,
            market_cap=501_396_000_000_000,
        ),
        mc.MarketCapEntry(
            rank=2,
            symbol="000660",
            name="SK하이닉스",
            market="KOSPI",
            shares=728_000_000,
            close=137_000,
            market_cap=99_736_000_000_000,
        ),
    ]
    path = tmp_path / "universe-2026-05-20.json"
    mc.save_universe(entries, path, top_n=2)

    loaded = mc.load_cached_universe(path)
    assert len(loaded) == 2
    assert loaded[0] == entries[0]
    assert loaded[1] == entries[1]


def test_load_returns_empty_when_file_missing(tmp_path: Path) -> None:
    assert mc.load_cached_universe(tmp_path / "nope.json") == []


def test_load_skips_corrupt_rows(tmp_path: Path) -> None:
    path = tmp_path / "universe-bad.json"
    path.write_text(
        '{"generated_at":"x","date":"x","top_n":2,"entries":['
        '{"rank":1,"symbol":"005930","name":"삼성전자","market":"KOSPI",'
        '"shares":1,"close":1,"market_cap":1},'
        '{"rank":"NO","symbol":"BAD"}'  # 필드 누락 → 스킵
        "]}",
        encoding="utf-8",
    )
    loaded = mc.load_cached_universe(path)
    assert len(loaded) == 1
    assert loaded[0].symbol == "005930"


def test_universe_path_for_uses_date(tmp_path: Path) -> None:
    path = mc.universe_path_for(tmp_path, when=date(2026, 5, 20))
    assert path == tmp_path / "universe-2026-05-20.json"


def test_resolve_query_date_skips_weekend(monkeypatch: pytest.MonkeyPatch) -> None:
    # 월요일(2026-05-18) base → 직전 거래일은 금요일(2026-05-15) 이어야 한다.
    # _resolve_query_date 는 base-1 부터 거꾸로 거래일 탐색.
    monday = date(2026, 5, 18)
    resolved = mc._resolve_query_date(monday)
    assert resolved.weekday() < 5  # 평일
    # 일요일/토요일을 건너뛰어 금요일에 도달
    assert resolved == date(2026, 5, 15)
