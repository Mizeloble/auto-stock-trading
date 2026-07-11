"""refresh_snapshot_joins — 차단일 리포트의 스냅샷 조인 소급 갱신 회귀.

배경: 게이트 차단일엔 러너가 개장 직후(~09:02) 리포트를 저장해 blocked_outcomes
가 스냅샷 1라운드(개장가) 값으로 얼어붙는다. 장 마감 후 이 함수가 하루치 스냅샷
으로 조인 필드만 다시 채우되, 나머지(마감 잔고 등)는 보존해야 한다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from auto_trading.reports.daily import (
    BlockedOutcome,
    DailyReport,
    RoundTrip,
    TradeRecord,
    load_daily_report,
    refresh_snapshot_joins,
    save_daily_report,
)

DATE = "2026-07-09"


def _write_snapshots(root: Path, *records: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"topcap-{DATE}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")


def _snapshot(hhmm: str, symbol: str, price: float, change_pct: float, rank: int = 1) -> dict:
    return {
        "ts": f"{DATE}T{hhmm}:06+09:00",
        "rank": rank,
        "symbol": symbol,
        "price": price,
        "change_pct": change_pct,
    }


def _blocked_day_report(**overrides: object) -> DailyReport:
    """차단일 리포트 재현 — 개장 1라운드 조인 값(0.0/None)으로 얼어붙은 상태."""
    base: dict = {
        "date": DATE,
        "mode": "day-trade",
        "dry_run": False,
        "discovery_chosen": [],
        "discovery_blocked": [("005930", "KOSPI_below_sma")],
        "market_gates": [],
        "buys": [],
        "liquidations": [],
        "error_count": 0,
        "note_ko": "게이트 차단",
        "blocked_outcomes": [
            BlockedOutcome(
                symbol="005930",
                name="삼성전자",
                reason="KOSPI_below_sma",
                intraday_pct=None,
                day_change_pct=0.0,
            )
        ],
        "closing_cash": 1_000_000,
    }
    base.update(overrides)
    return DailyReport(**base)  # type: ignore[arg-type]


def test_refresh_fills_blocked_outcomes_and_preserves_rest(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    snaps = tmp_path / "snapshots"
    save_daily_report(_blocked_day_report(), root=reports)
    _write_snapshots(
        snaps,
        _snapshot("09:00", "005930", 277500.0, 0.0),
        _snapshot("15:30", "005930", 289500.0, 4.32),
    )

    saved = refresh_snapshot_joins(DATE, report_root=reports, snapshot_root=snaps)

    assert saved == reports / f"daily-{DATE}.json"
    updated = load_daily_report(saved)
    outcome = updated.blocked_outcomes[0]
    assert outcome.day_change_pct == 4.32  # 개장가 0.0 → 종가 기준으로 교체
    assert outcome.intraday_pct is not None
    assert abs(outcome.intraday_pct - (289500.0 / 277500.0 - 1.0) * 100.0) < 1e-9
    assert outcome.name == "삼성전자"
    # 스냅샷과 무관한 필드는 보존.
    assert updated.closing_cash == 1_000_000
    assert updated.note_ko == "게이트 차단"


def test_refresh_returns_none_when_report_missing(tmp_path: Path) -> None:
    snaps = tmp_path / "snapshots"
    _write_snapshots(snaps, _snapshot("09:00", "005930", 100.0, 0.0))
    assert (
        refresh_snapshot_joins(DATE, report_root=tmp_path / "reports", snapshot_root=snaps) is None
    )


def test_refresh_returns_none_and_keeps_file_when_snapshots_missing(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    path = save_daily_report(_blocked_day_report(), root=reports)
    before = path.read_text(encoding="utf-8")

    result = refresh_snapshot_joins(DATE, report_root=reports, snapshot_root=tmp_path / "none")

    assert result is None
    assert path.read_text(encoding="utf-8") == before


def test_refresh_keeps_old_values_when_symbol_absent_from_snapshots(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    snaps = tmp_path / "snapshots"
    report = _blocked_day_report(
        blocked_outcomes=[
            BlockedOutcome(
                symbol="005930",
                name="삼성전자",
                reason="KOSPI_below_sma",
                intraday_pct=1.5,
                day_change_pct=2.0,
            )
        ]
    )
    save_daily_report(report, root=reports)
    # 다른 종목만 있는 스냅샷 — 005930 조인은 None 이 나오지만 기존 값을 지우면 안 된다.
    _write_snapshots(snaps, _snapshot("09:00", "000660", 100.0, 0.0))

    saved = refresh_snapshot_joins(DATE, report_root=reports, snapshot_root=snaps)

    assert saved is not None
    outcome = load_daily_report(saved).blocked_outcomes[0]
    assert outcome.intraday_pct == 1.5
    assert outcome.day_change_pct == 2.0


def test_refresh_enriches_buys_and_round_trips(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    snaps = tmp_path / "snapshots"
    report = _blocked_day_report(
        discovery_blocked=[],
        blocked_outcomes=[],
        buys=[
            TradeRecord(
                symbol="005930",
                side="buy",
                quantity=1,
                price=100.0,
                simulated=True,
                time_kst="09:10",
            )
        ],
        round_trips=[
            RoundTrip(
                symbol="005930",
                name=None,
                quantity=1,
                buy_price=100.0,
                sell_price=104.0,
                buy_time_kst="09:10",
                sell_time_kst="15:00",
                holding_minutes=350,
                pnl=4.0,
                pnl_pct=4.0,
                exit_reason="eod",
            )
        ],
    )
    save_daily_report(report, root=reports)
    _write_snapshots(
        snaps,
        _snapshot("09:10", "005930", 100.0, 1.0, rank=3),
        _snapshot("12:00", "005930", 110.0, 11.0, rank=3),
        _snapshot("15:00", "005930", 104.0, 5.0, rank=3),
    )

    saved = refresh_snapshot_joins(DATE, report_root=reports, snapshot_root=snaps)

    assert saved is not None
    updated = load_daily_report(saved)
    buy = updated.buys[0]
    assert buy.change_pct_at_entry == 1.0
    assert buy.rank_at_entry == 3
    rt = updated.round_trips[0]
    assert rt.mfe_pct == pytest.approx(10.0)  # 보유구간 최고 110 / 매수 100
    assert rt.mae_pct == pytest.approx(0.0)  # 보유구간 최저 100 / 매수 100
    # 조인 외 필드 보존.
    assert rt.pnl == 4.0
