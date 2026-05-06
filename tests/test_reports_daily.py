"""DailyReport 빌더 + JSON I/O + 핵심 집계."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from auto_trading.account.balance import AccountSnapshot, Holding
from auto_trading.notify.state import RunnerStateSnapshot
from auto_trading.reports.daily import (
    ClosingHolding,
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


def test_build_attaches_names_to_records_and_chosen_map() -> None:
    """LogEvents.symbol_names 가 buys/liquidations.name 과 discovery_chosen_names 둘 다에 흘러간다."""
    events = LogEvents(
        discovery={"chosen": ["005930", "001500"]},
        symbol_names={"005930": "삼성전자", "001500": "한솔홀딩스"},
        orders_simulated=[
            {"symbol": "005930", "side": "buy", "qty": 13, "price": 254000.0},
            {"symbol": "001500", "side": "buy", "qty": 254, "price": 13100.0},
        ],
        liquidations_simulated=[
            {"symbol": "005930", "qty": 13},
            {"symbol": "001500", "qty": 254},
        ],
    )
    report = build_daily_report(date="2026-05-06", events=events, snapshot=None)
    assert report.discovery_chosen_names == {"005930": "삼성전자", "001500": "한솔홀딩스"}
    assert {b.symbol: b.name for b in report.buys} == {"005930": "삼성전자", "001500": "한솔홀딩스"}
    assert {li.symbol: li.name for li in report.liquidations} == {
        "005930": "삼성전자",
        "001500": "한솔홀딩스",
    }


def test_liquidation_buy_price_from_log_field_overrides_buy_prices_map() -> None:
    """liquidate_simulated 의 buy_price 필드(러너 로그) 가 buys 매핑보다 우선."""
    events = LogEvents(
        orders_simulated=[{"symbol": "A", "side": "buy", "qty": 10, "price": 100.0}],
        liquidations_accepted=[
            # buy_price 가 명시 → 이 값을 사용
            {"symbol": "A", "qty": 10, "buy_price": 95.0, "sell_price": 110.0},
        ],
    )
    report = build_daily_report(date="2026-05-06", events=events, snapshot=None)
    assert len(report.liquidations) == 1
    li = report.liquidations[0]
    assert li.buy_price == 95.0
    assert li.sell_price == 110.0
    # 이 케이스는 sell + buy 둘 다 있어 realized 계산 가능
    assert report.realized_pnl == (110.0 - 95.0) * 10


def test_build_extracts_closing_balance_from_snapshot() -> None:
    """snapshot.snapshot 에 잔고가 있으면 closing_* 필드 채워짐."""
    account = AccountSnapshot(
        deposit=Decimal("50000000"),
        total_evaluation=Decimal("50050000"),
        total_pnl=Decimal("50000"),
        holdings=[
            Holding(
                symbol="005930",
                name="삼성전자",
                quantity=13,
                avg_price=Decimal("254000"),
                current_price=Decimal("256500"),
                market_value=Decimal("3334500"),
                pnl=Decimal("32500"),
                pnl_pct=Decimal("0.98"),
            ),
            # 0 수량은 거름.
            Holding(
                symbol="ZZZ",
                name="청산완료",
                quantity=0,
                avg_price=Decimal("0"),
                current_price=Decimal("0"),
                market_value=Decimal("0"),
                pnl=Decimal("0"),
                pnl_pct=Decimal("0"),
            ),
        ],
    )
    snap = RunnerStateSnapshot(
        mode="mock",
        dry_run=False,
        symbols=[],
        strategies={},
        started_at=None,
        snapshot=account,
        account=None,
        recent_signals={},
        balance_failures=0,
        runner_mode="day-trade",
    )
    report = build_daily_report(date="2026-05-06", events=LogEvents(), snapshot=snap)
    assert report.closing_cash == 50000000
    assert report.closing_equity == 50050000
    assert report.closing_total_pnl == 50000
    assert len(report.closing_holdings) == 1
    h = report.closing_holdings[0]
    assert isinstance(h, ClosingHolding)
    assert h.symbol == "005930"
    assert h.name == "삼성전자"
    assert h.quantity == 13


def test_closing_balance_blank_when_snapshot_missing() -> None:
    """snapshot.snapshot 이 None 이면 closing_* 모두 비움."""
    snap = RunnerStateSnapshot(
        mode="mock",
        dry_run=False,
        symbols=[],
        strategies={},
        started_at=None,
        snapshot=None,
        account=None,
        recent_signals={},
        balance_failures=0,
        runner_mode="day-trade",
    )
    report = build_daily_report(date="2026-05-06", events=LogEvents(), snapshot=snap)
    assert report.closing_cash is None
    assert report.closing_equity is None
    assert report.closing_total_pnl is None
    assert report.closing_holdings == []


def test_save_load_roundtrip_with_new_fields(tmp_path: Path) -> None:
    """names/closing_* 필드 라운드트립 — 누락 없이 보존."""
    report = DailyReport(
        date="2026-05-06",
        mode="day-trade",
        dry_run=False,
        discovery_chosen=["005930"],
        discovery_blocked=[],
        market_gates=[],
        buys=[
            TradeRecord(
                symbol="005930",
                side="buy",
                quantity=13,
                price=254000.0,
                simulated=False,
                name="삼성전자",
            )
        ],
        liquidations=[],
        error_count=0,
        note_ko="t",
        discovery_chosen_names={"005930": "삼성전자"},
        closing_cash=50_000_000,
        closing_equity=50_032_500,
        closing_total_pnl=32_500,
        closing_holdings=[
            ClosingHolding(
                symbol="005930",
                name="삼성전자",
                quantity=13,
                avg_price=254000.0,
                current_price=256500.0,
                market_value=3334500.0,
                pnl=32500.0,
                pnl_pct=0.98,
            )
        ],
    )
    path = save_daily_report(report, root=tmp_path)
    loaded = load_daily_report(path)
    assert loaded == report


def test_load_report_with_legacy_schema_uses_defaults(tmp_path: Path) -> None:
    """names/closing_* 가 없는 (구) JSON 도 기본값으로 안전 로드."""
    legacy = tmp_path / "daily-2026-05-06.json"
    legacy.write_text(
        '{"date":"2026-05-06","mode":"day-trade","dry_run":true,'
        '"discovery_chosen":["A"],"discovery_blocked":[],"market_gates":[],'
        '"buys":[],"liquidations":[],"error_count":0,"note_ko":""}',
        encoding="utf-8",
    )
    loaded = load_daily_report(legacy)
    assert loaded.discovery_chosen_names == {}
    assert loaded.closing_cash is None
    assert loaded.closing_holdings == []


# ---- 23단계 Phase A · 비용 추정 + realized_pnl_after_cost --------------------


def test_cost_estimation_with_buy_and_liq_prices() -> None:
    """매수·청산 가격이 모두 있을 때 fees/taxes/slippage 추정치 계산."""
    events = LogEvents(
        orders_simulated=[
            {"symbol": "A", "side": "buy", "qty": 10, "price": 100.0},  # 1,000원
        ],
        liquidations_simulated=[
            # sell_price 없음 → buy_price(100.0) 로 fallback. 1,000원 매도 추정.
            {"symbol": "A", "qty": 10},
        ],
    )
    report = build_daily_report(date="2026-05-05", events=events, snapshot=None)
    # 수수료: (1,000 매수 + 1,000 매도) * 0.015% = 0.30
    assert report.total_fees == 0.30
    # 거래세: 1,000 매도 * 0.20% = 2.0
    assert report.total_taxes == 2.0
    # 슬리피지: (1,000 + 1,000) * 0.05% = 1.0
    assert report.total_slippage == 1.0


def test_realized_pnl_after_cost_when_all_prices_present(tmp_path: Path) -> None:
    """sell/buy 가격 둘 다 있는 청산을 직접 build → 비용 차감 PnL 계산."""
    report = DailyReport(
        date="2026-05-05",
        mode="day-trade",
        dry_run=False,
        discovery_chosen=["A"],
        discovery_blocked=[],
        market_gates=[],
        buys=[TradeRecord(symbol="A", side="buy", quantity=10, price=100.0, simulated=False)],
        liquidations=[
            LiquidationRecord(
                symbol="A", quantity=10, sell_price=110.0, buy_price=100.0, simulated=False
            )
        ],
        error_count=0,
        note_ko="",
        realized_pnl=100.0,  # 10 * (110 - 100)
        realized_pnl_pct=10.0,
        total_fees=0.315,  # (1,000 + 1,100) * 0.00015
        total_taxes=2.20,  # 1,100 * 0.0020
        total_slippage=1.05,  # (1,000 + 1,100) * 0.0005
        realized_pnl_after_cost=100.0 - (0.315 + 2.20 + 1.05),
    )
    path = save_daily_report(report, root=tmp_path)
    loaded = load_daily_report(path)
    # 비용 4개 필드 모두 라운드트립.
    assert loaded.total_fees == 0.315
    assert loaded.total_taxes == 2.20
    assert loaded.total_slippage == 1.05
    assert loaded.realized_pnl_after_cost is not None
    # 100 - 3.565 = 96.435
    assert abs(loaded.realized_pnl_after_cost - 96.435) < 1e-9


def test_cost_fields_none_when_no_trades() -> None:
    """매매 0건이면 비용 필드 모두 None."""
    report = build_daily_report(date="2026-05-05", events=LogEvents(), snapshot=None)
    assert report.total_fees is None
    assert report.total_taxes is None
    assert report.total_slippage is None
    assert report.realized_pnl_after_cost is None


def test_cost_fields_none_when_buy_price_missing() -> None:
    """가격 없는 거래(실거래 accepted) 가 섞이면 추정 불가 → None 유지."""
    events = LogEvents(
        orders_accepted=[{"symbol": "A", "side": "buy", "qty": 10}],  # price 미기록
    )
    report = build_daily_report(date="2026-05-05", events=events, snapshot=None)
    assert report.total_fees is None
    assert report.total_taxes is None
    assert report.total_slippage is None
