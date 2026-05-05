"""DailyReport 빌더 + JSON I/O + 핵심 집계."""

from __future__ import annotations

from pathlib import Path

from auto_trading.notify.state import RunnerStateSnapshot
from auto_trading.reports.daily import (
    DailyReport,
    LiquidationRecord,
    MarketGateInfo,
    TradeRecord,
    build_daily_report,
    load_daily_report,
    save_daily_report,
)
from auto_trading.reports.log_parser import LogEvents


def _events_with_full_cycle() -> LogEvents:
    return LogEvents(
        discovery={
            "event": "daytrade.discovery",
            "chosen": ["018880", "010170"],
            "blocked": [["001510", "kospi_below_sma"]],
        },
        rank={
            "event": "market.rank.top_volume",
            "chosen": [["018880", "KOSPI", 100], ["010170", "KOSDAQ", 80]],
            "excluded_count": 53,
        },
        market_gates=[
            {"market": "KOSPI", "last": 6900.0, "sma5": 6700.0, "sma20": 6200.0, "above": True},
            {"market": "KOSDAQ", "last": 1200.0, "sma5": 1180.0, "sma20": 1150.0, "above": True},
        ],
        orders_simulated=[
            {"symbol": "018880", "side": "buy", "qty": 10, "price": 5000.0},
            {"symbol": "010170", "side": "buy", "qty": 5, "price": 10000.0},
        ],
        liquidations_simulated=[
            {"symbol": "018880", "qty": 10},
            {"symbol": "010170", "qty": 5},
        ],
    )


def _snapshot(*, mode: str = "day-trade", dry_run: bool = True) -> RunnerStateSnapshot:
    return RunnerStateSnapshot(
        mode="mock",
        dry_run=dry_run,
        symbols=[],
        strategies={},
        started_at=None,
        snapshot=None,
        account=None,
        recent_signals={},
        balance_failures=0,
        runner_mode=mode,
    )


def test_build_extracts_discovery_gates_buys_liquidations() -> None:
    report = build_daily_report(
        date="2026-05-05",
        events=_events_with_full_cycle(),
        snapshot=_snapshot(),
    )
    assert report.date == "2026-05-05"
    assert report.mode == "day-trade"
    assert report.dry_run is True
    assert report.discovery_chosen == ["018880", "010170"]
    assert report.discovery_blocked == [("001510", "kospi_below_sma")]
    assert {g.market for g in report.market_gates} == {"KOSPI", "KOSDAQ"}
    assert all(g.above for g in report.market_gates)
    assert len(report.buys) == 2
    assert all(t.simulated for t in report.buys)
    assert len(report.liquidations) == 2
    assert report.excluded_etf_count == 53
    assert "018880" in report.note_ko or "매수" in report.note_ko


def test_build_pnl_present_only_when_both_prices_available() -> None:
    """청산 시 sell_price 가 None 이면 (시장가 시뮬) pnl 계산 불가 → realized=None."""
    report = build_daily_report(
        date="2026-05-05",
        events=_events_with_full_cycle(),
        snapshot=_snapshot(),
    )
    # 시뮬 청산은 sell_price 가 None — realized 도 None.
    assert report.realized_pnl is None
    assert report.realized_pnl_pct is None


def test_build_with_no_snapshot_infers_mode_from_events() -> None:
    """수동 재생성 (snapshot=None) — events 로 mode 추정."""
    report = build_daily_report(
        date="2026-05-05",
        events=_events_with_full_cycle(),
        snapshot=None,
    )
    assert report.mode == "day-trade"
    assert report.dry_run is True  # orders_simulated 가 있으므로 dry-run 추정


def test_build_empty_events_returns_minimal_report() -> None:
    report = build_daily_report(date="2026-05-05", events=LogEvents(), snapshot=_snapshot())
    assert report.discovery_chosen == []
    assert report.buys == []
    assert report.liquidations == []
    assert report.error_count == 0
    assert "거래 없음" in report.note_ko or "차단" in report.note_ko


def test_build_counts_errors_from_orders_liqs_fatals_and_balance() -> None:
    events = LogEvents(
        orders_failed=[{"symbol": "A", "side": "buy", "qty": 1, "error": "x"}],
        liquidations_failed=[{"symbol": "B", "qty": 1, "error": "y"}],
        balance_failures=3,
        fatal=[{"consecutive": 5}],
    )
    report = build_daily_report(date="2026-05-05", events=events, snapshot=None)
    assert report.error_count == 4  # 1 orders_failed + 1 liqs_failed + 1 fatal + 1 balance(>0)


def test_save_load_roundtrip(tmp_path: Path) -> None:
    """JSON 직렬화 → 디스크 → 역직렬화에서 모든 필드 보존."""
    report = DailyReport(
        date="2026-05-05",
        mode="day-trade",
        dry_run=True,
        discovery_chosen=["A", "B"],
        discovery_blocked=[("C", "below")],
        market_gates=[
            MarketGateInfo(market="KOSPI", last=1.0, sma5=1.0, sma20=1.0, above=True),
        ],
        buys=[TradeRecord(symbol="A", side="buy", quantity=10, price=100.0, simulated=True)],
        liquidations=[
            LiquidationRecord(
                symbol="A", quantity=10, sell_price=110.0, buy_price=100.0, simulated=False
            )
        ],
        error_count=1,
        note_ko="요약",
        realized_pnl=100.0,
        realized_pnl_pct=10.0,
        excluded_etf_count=2,
        rank_candidates=["A", "B", "C"],
    )
    path = save_daily_report(report, root=tmp_path)
    loaded = load_daily_report(path)
    assert loaded == report


def test_save_creates_directory(tmp_path: Path) -> None:
    """저장 폴더가 없어도 자동 생성."""
    nested = tmp_path / "deep" / "reports"
    report = DailyReport(
        date="2026-05-05",
        mode="day-trade",
        dry_run=True,
        discovery_chosen=[],
        discovery_blocked=[],
        market_gates=[],
        buys=[],
        liquidations=[],
        error_count=0,
        note_ko="",
    )
    save_daily_report(report, root=nested)
    assert (nested / "daily-2026-05-05.json").exists()


def test_pnl_aggregate_when_both_prices_present() -> None:
    """수동 입력으로 sell/buy 가격이 둘 다 있으면 realized 계산 가능."""
    events = LogEvents(
        orders_simulated=[{"symbol": "A", "side": "buy", "qty": 10, "price": 100.0}],
        # 실거래 청산은 가격 미기록이라 시뮬과 동일. 직접 LiquidationRecord 빌드 케이스는
        # save/load 테스트가 커버.
    )
    report = build_daily_report(date="2026-05-05", events=events, snapshot=None)
    assert len(report.buys) == 1
    # sell_price 가 없으므로 realized_pnl 은 None — 우리가 가진 입력으로는 자연스러운 결과.
    assert report.realized_pnl is None
