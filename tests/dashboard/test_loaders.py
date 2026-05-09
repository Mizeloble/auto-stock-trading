"""dashboard.loaders — 빈/깨진/path traversal 엣지 케이스."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from auto_trading.dashboard import loaders


def _write_daily(reports_root: Path, date: str, **overrides: object) -> Path:
    base: dict[str, object] = {
        "date": date,
        "mode": "day-trade",
        "dry_run": True,
        "discovery_chosen": ["005930"],
        "discovery_blocked": [],
        "market_gates": [],
        "buys": [
            {
                "symbol": "005930",
                "side": "buy",
                "quantity": 10,
                "price": 70000.0,
                "simulated": True,
                "name": "삼성전자",
            }
        ],
        "liquidations": [
            {
                "symbol": "005930",
                "quantity": 10,
                "sell_price": 71000.0,
                "buy_price": 70000.0,
                "simulated": True,
                "name": "삼성전자",
            }
        ],
        "error_count": 0,
        "note_ko": "테스트",
        "realized_pnl": 10000.0,
        "realized_pnl_pct": 1.43,
        "excluded_etf_count": 0,
        "rank_candidates": [],
        "discovery_chosen_names": {"005930": "삼성전자"},
        "closing_cash": 9_900_000,
        "closing_equity": 10_000_000,
        "closing_total_pnl": 100_000,
        "closing_holdings": [],
        "total_fees": 100.0,
        "total_taxes": 1420.0,
        "total_slippage": 70.0,
        "realized_pnl_after_cost": 8410.0,
    }
    base.update(overrides)
    reports_root.mkdir(parents=True, exist_ok=True)
    path = reports_root / f"daily-{date}.json"
    path.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    return path


def test_list_daily_dates_empty_dir(tmp_path: Path) -> None:
    assert loaders.list_daily_dates(tmp_path / "missing") == []
    assert loaders.list_daily_dates(tmp_path) == []


def test_list_daily_dates_sorts_desc_and_filters_unrelated(tmp_path: Path) -> None:
    _write_daily(tmp_path, "2026-05-01")
    _write_daily(tmp_path, "2026-05-03")
    _write_daily(tmp_path, "2026-05-02")
    (tmp_path / "insight-weekly-2026-05-03.json").write_text("{}", encoding="utf-8")
    (tmp_path / "daily-not-a-date.json").write_text("{}", encoding="utf-8")

    assert loaders.list_daily_dates(tmp_path) == ["2026-05-03", "2026-05-02", "2026-05-01"]


def test_load_daily_round_trips_payload(tmp_path: Path) -> None:
    _write_daily(tmp_path, "2026-05-05")
    payload = loaders.load_daily(tmp_path, "2026-05-05")
    assert payload is not None
    assert payload["date"] == "2026-05-05"
    assert payload["realized_pnl"] == 10000.0
    assert payload["discovery_blocked"] == []
    assert payload["closing_equity"] == 10_000_000


def test_load_daily_invalid_date_string(tmp_path: Path) -> None:
    assert loaders.load_daily(tmp_path, "../etc/passwd") is None
    assert loaders.load_daily(tmp_path, "2026-13-99") is None
    assert loaders.load_daily(tmp_path, "") is None


def test_load_daily_corrupt_json_returns_none(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "daily-2026-05-05.json").write_text("not json", encoding="utf-8")
    assert loaders.load_daily(tmp_path, "2026-05-05") is None


def test_equity_series_accumulates_pnl_in_date_order(tmp_path: Path) -> None:
    _write_daily(
        tmp_path,
        "2026-05-01",
        realized_pnl=5000.0,
        realized_pnl_after_cost=4000.0,
        closing_equity=10_004_000,
    )
    _write_daily(
        tmp_path,
        "2026-05-02",
        realized_pnl=-2000.0,
        realized_pnl_after_cost=-3000.0,
        closing_equity=10_001_000,
    )
    _write_daily(
        tmp_path,
        "2026-05-03",
        realized_pnl=None,
        realized_pnl_after_cost=None,
        closing_equity=10_001_000,
    )

    series = loaders.equity_series(tmp_path)
    assert [p["date"] for p in series] == ["2026-05-01", "2026-05-02", "2026-05-03"]
    assert series[0]["daily_pnl"] == 4000.0
    assert series[0]["cumulative_pnl"] == 4000.0
    assert series[1]["daily_pnl"] == -3000.0
    assert series[1]["cumulative_pnl"] == 1000.0
    assert series[2]["daily_pnl"] is None
    assert series[2]["cumulative_pnl"] == 1000.0


def test_list_backtests_empty_and_skips_missing_report(tmp_path: Path) -> None:
    assert loaders.list_backtests(tmp_path / "absent") == []

    backtests_root = tmp_path / "backtests"
    backtests_root.mkdir()
    (backtests_root / "no_report").mkdir()
    (backtests_root / "no_report" / "equity.csv").write_text("date,equity\n", encoding="utf-8")

    real = backtests_root / "sma_005930_20260505_120000"
    real.mkdir()
    (real / "report.json").write_text(json.dumps({"sharpe": 1.2}), encoding="utf-8")

    runs = loaders.list_backtests(backtests_root)
    assert [r["name"] for r in runs] == ["sma_005930_20260505_120000"]


def test_load_backtest_full_payload(tmp_path: Path) -> None:
    backtests_root = tmp_path / "backtests"
    run_dir = backtests_root / "sma_005930_20260505_120000"
    run_dir.mkdir(parents=True)
    (run_dir / "report.json").write_text(
        json.dumps({"total_return_pct": 12.5, "sharpe": 1.4, "num_trades": 4}),
        encoding="utf-8",
    )
    with (run_dir / "equity.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "equity"])
        w.writerow(["2026-05-01", "10000000"])
        w.writerow(["2026-05-02T15:30:00", "10125000"])
    with (run_dir / "trades.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "symbol", "side", "price", "quantity", "fee", "tax"])
        w.writerow(["2026-05-01T09:00:00", "005930", "buy", "70000", "10", "10", "0"])
        w.writerow(["2026-05-02T09:00:00", "005930", "sell", "71000", "10", "10", "1420"])

    payload = loaders.load_backtest(backtests_root, "sma_005930_20260505_120000")
    assert payload is not None
    assert payload["report"]["sharpe"] == 1.4
    assert payload["equity"] == [
        {"date": "2026-05-01", "equity": 10_000_000.0},
        {"date": "2026-05-02", "equity": 10_125_000.0},
    ]
    assert len(payload["trades"]) == 2
    assert payload["trades"][1]["side"] == "sell"
    assert len(payload["symbol_pnl"]) == 1
    sym = payload["symbol_pnl"][0]
    assert sym["symbol"] == "005930"
    assert sym["trades"] == 1
    assert sym["pnl"] == pytest.approx(10 * 1000 - (10 + 10 + 1420), rel=1e-6)


def test_load_backtest_rejects_traversal_and_missing(tmp_path: Path) -> None:
    backtests_root = tmp_path / "backtests"
    backtests_root.mkdir()
    assert loaders.load_backtest(backtests_root, "../escape") is None
    assert loaders.load_backtest(backtests_root, "no_such_run") is None
    assert loaders.load_backtest(backtests_root, "with/slash") is None


def test_load_backtest_handles_missing_csv_files(tmp_path: Path) -> None:
    run_dir = tmp_path / "run_only_report"
    run_dir.mkdir()
    (run_dir / "report.json").write_text(json.dumps({"sharpe": 0.0}), encoding="utf-8")

    payload = loaders.load_backtest(tmp_path, "run_only_report")
    assert payload is not None
    assert payload["equity"] == []
    assert payload["trades"] == []
    assert payload["symbol_pnl"] == []
