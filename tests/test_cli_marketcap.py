"""`auto-trade build-marketcap-universe` 와 `snapshot-top200` CLI 회귀.

기존 `test_cli_latency.py` 패턴 — `typer.testing.CliRunner` + `isolated_cwd`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Generator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import structlog
from typer.testing import CliRunner

from auto_trading._common import KST
from auto_trading.cli import app
from auto_trading.market.marketcap import MarketCapEntry
from auto_trading.market.quote import Quote


@pytest.fixture(autouse=True)
def _reset_structlog() -> Generator[None]:
    yield
    logging.getLogger().handlers.clear()
    structlog.reset_defaults()


def _fake_entries(n: int = 3) -> list[MarketCapEntry]:
    base = [
        ("005930", "삼성전자", "KOSPI", 500_000_000_000_000),
        ("000660", "SK하이닉스", "KOSPI", 100_000_000_000_000),
        ("247540", "에코프로비엠", "KOSDAQ", 30_000_000_000_000),
    ]
    return [
        MarketCapEntry(
            rank=i + 1, symbol=sym, name=name, market=market,
            shares=1_000_000, close=10_000, market_cap=cap,
        )
        for i, (sym, name, market, cap) in enumerate(base[:n])
    ]


def test_build_marketcap_universe_writes_cache(
    isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from auto_trading.cli import _ops  # 데코레이터 등록 보장
    from auto_trading.market import marketcap

    monkeypatch.setattr(
        marketcap, "compute_top_marketcap_universe", lambda **kw: _fake_entries(3)
    )

    out_dir = isolated_cwd / "snapshots"
    result = CliRunner().invoke(
        app,
        ["build-marketcap-universe", "--top-n", "3", "--out-dir", str(out_dir)],
    )

    assert result.exit_code == 0, result.output
    assert "시총 마스터 저장" in result.output
    # 파일 생성 + 내용 확인
    files = list(out_dir.glob("universe-*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["top_n"] == 3
    assert len(data["entries"]) == 3
    assert data["entries"][0]["symbol"] == "005930"


def test_build_marketcap_universe_fails_when_empty(
    isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from auto_trading.market import marketcap

    monkeypatch.setattr(marketcap, "compute_top_marketcap_universe", lambda **kw: [])

    result = CliRunner().invoke(
        app, ["build-marketcap-universe", "--out-dir", str(isolated_cwd / "snap")]
    )
    assert result.exit_code == 1
    assert "시총 데이터를 받지 못했습니다" in result.output


def _seed_universe(out_dir: Path, when: date | None = None) -> Path:
    """오늘 날짜로 universe JSON 시드."""
    from auto_trading.market.marketcap import save_universe, universe_path_for

    out_dir.mkdir(parents=True, exist_ok=True)
    target = when or datetime.now(KST).date()
    path = universe_path_for(out_dir, when=target)
    save_universe(_fake_entries(3), path, top_n=3)
    return path


def test_snapshot_top200_appends_jsonl(
    isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from auto_trading import cli as cli_pkg
    from auto_trading.monitor import price_snapshot as ps

    universe_dir = isolated_cwd / "snap"
    _seed_universe(universe_dir)

    # 가짜 키움 client (with async context manager) — 실제 토큰 발급 안 함.
    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(cli_pkg, "KiwoomRestClient", _FakeClient)

    async def fake_get_quote(_client, sym):
        return Quote(symbol=sym, price=Decimal("70000"), change=Decimal("0"),
                     change_pct=Decimal("0"), volume=1)

    monkeypatch.setattr(ps, "get_quote", fake_get_quote)

    async def fake_fetch_index(_client, market):
        return 2730.12 if market == "KOSPI" else 850.45

    monkeypatch.setattr(ps, "fetch_latest_index_price", fake_fetch_index)

    result = CliRunner().invoke(
        app,
        [
            "snapshot-top200",
            "--universe-dir", str(universe_dir),
            "--out-dir", str(universe_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "스냅샷 1라운드" in result.output
    assert "지수 스냅샷" in result.output

    jsonl_files = list(universe_dir.glob("topcap-*.jsonl"))
    assert len(jsonl_files) == 1
    lines = jsonl_files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3

    # 지수 파일도 같은 라운드에 2줄 생성 (KOSPI, KOSDAQ)
    index_files = list(universe_dir.glob("indices-*.jsonl"))
    assert len(index_files) == 1
    index_lines = index_files[0].read_text(encoding="utf-8").splitlines()
    assert len(index_lines) == 2


def test_snapshot_top200_errors_when_no_universe(isolated_cwd: Path) -> None:
    """universe 캐시가 전혀 없으면 친화적 에러 메시지 + exit 1."""
    result = CliRunner().invoke(
        app,
        [
            "snapshot-top200",
            "--universe-dir", str(isolated_cwd / "empty"),
            "--out-dir", str(isolated_cwd / "empty"),
        ],
    )
    assert result.exit_code == 1
    assert "universe 캐시를 찾지 못했습니다" in result.output
    assert "build-marketcap-universe" in result.output


def test_snapshot_top200_falls_back_to_yesterday(
    isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """오늘 universe 없으면 어제 캐시로 폴백."""
    from auto_trading import cli as cli_pkg
    from auto_trading.monitor import price_snapshot as ps

    from datetime import timedelta as _td

    universe_dir = isolated_cwd / "snap"
    yesterday = datetime.now(KST).date() - _td(days=1)
    _seed_universe(universe_dir, when=yesterday)

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(cli_pkg, "KiwoomRestClient", _FakeClient)

    async def fake_get_quote(_client, sym):
        return Quote(symbol=sym, price=Decimal("1"), change=Decimal("0"),
                     change_pct=Decimal("0"), volume=1)

    monkeypatch.setattr(ps, "get_quote", fake_get_quote)

    async def fake_fetch_index(_client, market):
        return 2730.12 if market == "KOSPI" else 850.45

    monkeypatch.setattr(ps, "fetch_latest_index_price", fake_fetch_index)

    result = CliRunner().invoke(
        app,
        [
            "snapshot-top200",
            "--universe-dir", str(universe_dir),
            "--out-dir", str(universe_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "폴백 사용" in result.output
