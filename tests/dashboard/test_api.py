"""dashboard FastAPI 라우터 — TestClient 스모크."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auto_trading.dashboard.app import create_app


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    reports = tmp_path / "reports"
    backtests = tmp_path / "backtests"
    reports.mkdir()
    backtests.mkdir()
    app = create_app(reports_root=reports, backtests_root=backtests)
    return TestClient(app)


@pytest.fixture
def populated_client(tmp_path: Path) -> TestClient:
    reports = tmp_path / "reports"
    backtests = tmp_path / "backtests"
    reports.mkdir()
    backtests.mkdir()
    payload = {
        "date": "2026-05-05",
        "mode": "day-trade",
        "dry_run": True,
        "discovery_chosen": ["005930"],
        "discovery_blocked": [],
        "market_gates": [],
        "buys": [],
        "liquidations": [],
        "error_count": 0,
        "note_ko": "거래 없음.",
        "realized_pnl": None,
        "realized_pnl_pct": None,
        "excluded_etf_count": 0,
        "rank_candidates": [],
        "discovery_chosen_names": {},
        "closing_cash": 10_000_000,
        "closing_equity": 10_000_000,
        "closing_total_pnl": 0,
        "closing_holdings": [],
        "total_fees": None,
        "total_taxes": None,
        "total_slippage": None,
        "realized_pnl_after_cost": None,
    }
    (reports / "daily-2026-05-05.json").write_text(json.dumps(payload), encoding="utf-8")

    run_dir = backtests / "sma_005930_20260505_120000"
    run_dir.mkdir()
    (run_dir / "report.json").write_text(
        json.dumps({"total_return_pct": 12.5, "sharpe": 1.4}),
        encoding="utf-8",
    )
    with (run_dir / "equity.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "equity"])
        w.writerow(["2026-05-01", "10000000"])
    with (run_dir / "trades.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "symbol", "side", "price", "quantity", "fee", "tax"])

    app = create_app(reports_root=reports, backtests_root=backtests)
    return TestClient(app)


def test_healthz(client: TestClient) -> None:
    res = client.get("/api/healthz")
    assert res.status_code == 200
    assert res.json() == {"ok": True}


def test_empty_lists_when_no_data(client: TestClient) -> None:
    assert client.get("/api/daily").json() == {"dates": []}
    assert client.get("/api/equity").json() == []
    assert client.get("/api/backtests").json() == {"runs": []}


def test_daily_404_for_unknown_date(client: TestClient) -> None:
    res = client.get("/api/daily/2026-01-01")
    assert res.status_code == 404


def test_backtest_404_for_unknown_run(client: TestClient) -> None:
    res = client.get("/api/backtests/no_such_run")
    assert res.status_code == 404


def test_populated_endpoints(populated_client: TestClient) -> None:
    res = populated_client.get("/api/daily")
    assert res.json() == {"dates": ["2026-05-05"]}

    res = populated_client.get("/api/daily/2026-05-05")
    assert res.status_code == 200
    body = res.json()
    assert body["date"] == "2026-05-05"
    assert body["closing_equity"] == 10_000_000

    res = populated_client.get("/api/equity")
    points = res.json()
    assert len(points) == 1
    assert points[0]["date"] == "2026-05-05"

    res = populated_client.get("/api/backtests")
    assert res.json()["runs"][0]["name"] == "sma_005930_20260505_120000"

    res = populated_client.get("/api/backtests/sma_005930_20260505_120000")
    body = res.json()
    assert body["report"]["sharpe"] == 1.4
    assert body["equity"][0]["equity"] == 10_000_000.0


def test_pages_render_html(populated_client: TestClient) -> None:
    for path in ("/", "/daily", "/backtests"):
        res = populated_client.get(path)
        assert res.status_code == 200
        assert "auto-trade" in res.text
        assert "/static/app.js" in res.text
