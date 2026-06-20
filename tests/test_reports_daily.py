"""DailyReport 빌더 + JSON I/O + 핵심 집계."""

from __future__ import annotations

from datetime import UTC
from decimal import Decimal
from pathlib import Path

import pytest

from auto_trading.account.balance import AccountSnapshot, Holding
from auto_trading.notify.state import RunnerStateSnapshot
from auto_trading.reports.daily import (
    BlockedOutcome,
    ClosingHolding,
    DailyReport,
    LiquidationRecord,
    MarketGateInfo,
    RoundTrip,
    TimelineEvent,
    TradeRecord,
    build_daily_report,
    load_daily_report,
    save_daily_report,
)
from auto_trading.reports.log_parser import LogEvents
from auto_trading.risk.manager import AccountState


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


# 2026-05-11 회귀: note_ko 가 buys 와 일치해야 한다.


def test_note_ko_uses_actual_buys_not_discovery_count() -> None:
    """기존 버그: 발굴 3건 + 실제 매수 1건일 때 note_ko 가 "매수 3건" 으로 잘못 표기.

    수정: 매수는 실제 체결(buys) 기준으로 세고, 발굴 카운트와 분리.
    """
    events = LogEvents(
        discovery={
            "event": "daytrade.discovery",
            "chosen": ["005930", "028050", "006360"],  # 3건 발굴
            "blocked": [],
        },
        orders_accepted=[
            # 실제 체결은 005930 1건뿐.
            {"symbol": "005930", "side": "buy", "qty": 12, "price": 275500.0},
        ],
    )
    report = build_daily_report(date="2026-05-07", events=events, snapshot=None)
    assert len(report.buys) == 1
    assert "매수 1건" in report.note_ko
    # 발굴 카운트(3)가 매수 카운트로 새어나오면 안 됨.
    assert "매수 3건" not in report.note_ko


def test_reentry_blocked_count_from_signal_rejected() -> None:
    """매수여력 부족(일 매수대금 한도 소진 등) 거부만 세고, 다른 거부는 제외.

    2026-06-19 — "장 초반 쏠림" 가시화. 개장 웨이브가 한도를 소진하면 오후
    재진입이 budget too small / quantity 0 로 막힌다. 이를 리포트에 카운트.
    """
    events = LogEvents(
        signal_rejected=[
            {"symbol": "005930", "reason": "budget too small (6420)"},
            {"symbol": "010690", "reason": "quantity 0 after sizing"},
            {"symbol": "100090", "reason": "daily notional limit reached (10,000,000/10,000,000)"},
            # 매수여력과 무관한 거부 — 세면 안 됨.
            {
                "symbol": "000660",
                "reason": "correlation guard (000660 vs 005930 corr=0.80 >= 0.70)",
            },
            {"symbol": "005930", "reason": "daily loss limit hit (-3.20%)"},
        ],
    )
    report = build_daily_report(date="2026-06-17", events=events, snapshot=None)
    assert report.reentry_blocked_count == 3
    # snapshot 없는 수동 재생성은 today_notional_used 미가용(None).
    assert report.today_notional_used is None


def test_note_ko_says_discovery_when_buys_empty_but_chosen_present() -> None:
    """2026-05-11 회귀: 발굴 3건 + 매수 0건(kill switch 등) + 청산 1건.

    기존 메모는 "매수 3건 ... · 청산 1건" 으로 잘못 적었다. 새 동작은
    실제 buys 가 0건임을 명시해야 한다.
    """
    events = LogEvents(
        discovery={
            "event": "daytrade.discovery",
            "chosen": ["005930", "336570", "046890"],
            "blocked": [],
        },
        liquidations_accepted=[
            {"symbol": "005930", "qty": 6, "buy_price": 275500.0},
        ],
    )
    report = build_daily_report(date="2026-05-11", events=events, snapshot=None)
    assert report.buys == []
    assert len(report.liquidations) == 1
    assert "발굴 3건" in report.note_ko
    assert "체결 0" in report.note_ko
    assert "청산 1건" in report.note_ko
    assert "매수 3건" not in report.note_ko


def test_note_ko_dedupes_buy_symbols() -> None:
    """같은 종목 분할 매수는 1건으로 — 발굴 카운트와 일관성."""
    events = LogEvents(
        discovery={
            "event": "daytrade.discovery",
            "chosen": ["005930"],
            "blocked": [],
        },
        orders_accepted=[
            {"symbol": "005930", "side": "buy", "qty": 5, "price": 275500.0},
            {"symbol": "005930", "side": "buy", "qty": 7, "price": 280000.0},
        ],
    )
    report = build_daily_report(date="2026-05-07", events=events, snapshot=None)
    assert len(report.buys) == 2  # buys 배열엔 둘 다 보존
    assert "매수 1건" in report.note_ko  # 메모에선 종목 기준 1건
    assert "005930" in report.note_ko


# 2026-05-11 사용자 피드백 — timeline 빌더 회귀.


def _ts(hh: int, mm: int) -> str:
    """KST hh:mm 으로 나오게 하는 UTC ISO 8601. KST 09:00 = UTC 00:00, KST 08:55 = UTC 전날 23:55."""
    from datetime import datetime, timedelta, timezone

    kst = timezone(timedelta(hours=9))
    return datetime(2026, 5, 11, hh, mm, tzinfo=kst).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_timeline_collects_and_sorts_event_types() -> None:
    """발굴·매수·부분 익절·kill switch·청산 시작을 시각 순으로 한 줄씩."""
    events = LogEvents(
        symbol_names={"005930": "삼성전자"},
        discovery={
            "event": "daytrade.discovery",
            "chosen": ["005930"],
            "blocked": [],
            "timestamp": _ts(8, 55),
        },
        orders_accepted=[
            {
                "symbol": "005930",
                "side": "buy",
                "qty": 12,
                "price": 275500.0,
                "timestamp": _ts(9, 0),
            }
        ],
        partial_takes=[
            {
                "symbol": "005930",
                "price": 284500.0,
                "avg_price": 275500.0,
                "gain_pct": 3.27,
                "qty_to_sell": 6,
                "qty_remaining": 6,
                "timestamp": _ts(9, 1),
            }
        ],
        kill_switches=[
            {
                "today_change": -0.035,
                "daily_loss_limit": 0.03,
                "timestamp": _ts(9, 1),
            }
        ],
        liquidate_starts=[
            {
                "reason": "daily_loss_kill",
                "symbols": ["005930"],
                "dry_run": False,
                "timestamp": _ts(9, 1),
            }
        ],
    )
    report = build_daily_report(date="2026-05-11", events=events, snapshot=None)
    times = [(ev.time_kst, ev.kind) for ev in report.timeline]
    assert times == [
        ("08:55", "discovery"),
        ("09:00", "buy"),
        ("09:01", "partial_take"),
        ("09:01", "kill_switch"),
        ("09:01", "liquidate"),
    ]
    # 텍스트에 종목명·수량·가격 포함.
    buy_text = next(ev.text for ev in report.timeline if ev.kind == "buy")
    assert "005930 삼성전자" in buy_text and "12주" in buy_text and "275,500원" in buy_text
    pt_text = next(ev.text for ev in report.timeline if ev.kind == "partial_take")
    assert "+3.27%" in pt_text and "6주" in pt_text
    ks_text = next(ev.text for ev in report.timeline if ev.kind == "kill_switch")
    assert "🛑" in ks_text and "-3.50%" in ks_text  # today_change=-0.035 → -3.50%
    liq_text = next(ev.text for ev in report.timeline if ev.kind == "liquidate")
    assert "일일 손실 한도" in liq_text and "005930 삼성전자" in liq_text


def test_timeline_includes_all_sell_stop_liquidation_fills() -> None:
    """2026-06-15 회귀: timeline 이 partial_take 가드만 잡고 손절·트레일링·일반 매도·
    청산 체결을 통째로 빠뜨려 매도 흐름이 착시됐다.

    실제 그날 흐름 재현: 매수 3건 후 손절(093370)·트레일링(005930)·부분익절(006800)·
    트레일링(006800) 4건의 매도가 orders_accepted(side=sell) 로 체결됐고, 각각
    position_guard 가드 이벤트가 (종목, 체결가) 로 매칭된다. 4건 모두 시각순으로
    timeline 에 들어가고, 가드 사유로 라벨링되며, 중복(가드 줄 + 매도 줄) 이 없어야 한다.
    """
    events = LogEvents(
        symbol_names={"093370": "후성", "005930": "삼성전자", "006800": "미래에셋증권"},
        discovery={
            "event": "daytrade.discovery",
            "chosen": ["005930", "006800", "093370"],
            "blocked": [],
            "timestamp": _ts(9, 1),
        },
        orders_accepted=[
            {
                "symbol": "005930",
                "side": "buy",
                "qty": 10,
                "price": 339500.0,
                "timestamp": _ts(9, 10),
            },
            {
                "symbol": "006800",
                "side": "buy",
                "qty": 66,
                "price": 51500.0,
                "timestamp": _ts(9, 10),
            },
            {
                "symbol": "093370",
                "side": "buy",
                "qty": 159,
                "price": 20100.0,
                "timestamp": _ts(9, 10),
            },
            # 손절 매도.
            {
                "symbol": "093370",
                "side": "sell",
                "qty": 159,
                "price": 19660.0,
                "timestamp": _ts(9, 11),
            },
            # 트레일링 매도.
            {
                "symbol": "005930",
                "side": "sell",
                "qty": 10,
                "price": 336500.0,
                "timestamp": _ts(9, 13),
            },
            # 부분 익절 매도.
            {
                "symbol": "006800",
                "side": "sell",
                "qty": 33,
                "price": 52600.0,
                "timestamp": _ts(9, 18),
            },
            # 트레일링 매도.
            {
                "symbol": "006800",
                "side": "sell",
                "qty": 33,
                "price": 52800.0,
                "timestamp": _ts(9, 26),
            },
        ],
        stop_losses=[
            {
                "symbol": "093370",
                "price": 19660.0,
                "avg_price": 20197.0,
                "drop_pct": 2.659,
                "timestamp": _ts(9, 11),
            },
        ],
        partial_takes=[
            {
                "symbol": "006800",
                "price": 52600.0,
                "avg_price": 51404.0,
                "gain_pct": 2.327,
                "qty_to_sell": 33,
                "qty_remaining": 33,
                "timestamp": _ts(9, 18),
            },
        ],
        trailings=[
            {
                "symbol": "005930",
                "price": 336500.0,
                "highest": 340000.0,
                "drop_pct": 1.029,
                "timestamp": _ts(9, 13),
            },
            {
                "symbol": "006800",
                "price": 52800.0,
                "highest": 53300.0,
                "drop_pct": 0.938,
                "timestamp": _ts(9, 26),
            },
        ],
    )
    report = build_daily_report(date="2026-06-15", events=events, snapshot=None)
    timeline = report.timeline

    # 매도/손절/청산 계열 줄: 정확히 4건 (체결 1건당 1줄, 가드 줄 중복 없음).
    sell_kinds = {"sell", "stop_loss", "trailing", "partial_take"}
    sell_rows = [ev for ev in timeline if ev.kind in sell_kinds]
    assert len(sell_rows) == 4
    assert [(ev.time_kst, ev.kind) for ev in sell_rows] == [
        ("09:11", "stop_loss"),
        ("09:13", "trailing"),
        ("09:18", "partial_take"),
        ("09:26", "trailing"),
    ]
    # 사유·수량·가격·등락률 라벨링.
    by_time = {ev.time_kst: ev.text for ev in sell_rows}
    assert "🛑 손절" in by_time["09:11"] and "159주" in by_time["09:11"]
    assert "19,660원" in by_time["09:11"] and "-2.66%" in by_time["09:11"]
    assert "트레일링 청산" in by_time["09:13"] and "336,500원" in by_time["09:13"]
    assert "부분 익절" in by_time["09:18"] and "+2.33%" in by_time["09:18"]
    # 전체 시각순 정렬: 발굴 → 매수 3 → 매도 4.
    assert [ev.kind for ev in timeline] == [
        "discovery",
        "buy",
        "buy",
        "buy",
        "stop_loss",
        "trailing",
        "partial_take",
        "trailing",
    ]


def test_timeline_plain_sell_without_guard_event() -> None:
    """가드 이벤트 없는 매도(시그널·수동 청산)는 '매도' 로 표시된다."""
    events = LogEvents(
        symbol_names={"005930": "삼성전자"},
        orders_accepted=[
            {
                "symbol": "005930",
                "side": "buy",
                "qty": 6,
                "price": 270000.0,
                "timestamp": _ts(9, 5),
            },
            {
                "symbol": "005930",
                "side": "sell",
                "qty": 6,
                "price": 280000.0,
                "timestamp": _ts(9, 30),
            },
        ],
    )
    report = build_daily_report(date="2026-06-15", events=events, snapshot=None)
    sell_rows = [ev for ev in report.timeline if ev.kind == "sell"]
    assert len(sell_rows) == 1
    assert sell_rows[0].time_kst == "09:30"
    assert "매도 005930 삼성전자 6주" in sell_rows[0].text and "280,000원" in sell_rows[0].text


def test_timeline_liquidation_fills_included() -> None:
    """EOD 강제청산 체결(liquidations_accepted)도 '청산 체결' 줄로 들어간다."""
    events = LogEvents(
        symbol_names={"005930": "삼성전자"},
        liquidations_accepted=[
            {"symbol": "005930", "qty": 6, "sell_price": 280000.0, "timestamp": _ts(15, 20)},
        ],
    )
    report = build_daily_report(date="2026-06-15", events=events, snapshot=None)
    liq_rows = [ev for ev in report.timeline if ev.kind == "liquidate"]
    assert len(liq_rows) == 1
    assert liq_rows[0].time_kst == "15:20"
    assert "청산 체결 005930 삼성전자 6주" in liq_rows[0].text and "280,000원" in liq_rows[0].text


def test_timeline_skips_events_without_timestamp() -> None:
    events = LogEvents(
        discovery={"event": "daytrade.discovery", "chosen": ["A"]},  # timestamp 없음
        orders_accepted=[
            {"symbol": "A", "side": "buy", "qty": 1, "price": 1.0},  # timestamp 없음
        ],
    )
    report = build_daily_report(date="2026-05-11", events=events, snapshot=None)
    assert report.timeline == []


def test_timeline_round_trip_through_json(tmp_path: Path) -> None:
    """저장·로드 라운드트립에서 timeline 도 보존."""
    report = DailyReport(
        date="2026-05-11",
        mode="day-trade",
        dry_run=False,
        discovery_chosen=[],
        discovery_blocked=[],
        market_gates=[],
        buys=[],
        liquidations=[],
        error_count=0,
        note_ko="",
        timeline=[TimelineEvent(time_kst="09:01", kind="buy", text="매수 A 1주 @1원")],
    )
    path = save_daily_report(report, root=tmp_path)
    loaded = load_daily_report(path)
    assert loaded.timeline == report.timeline


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


def test_save_load_roundtrip_preserves_every_field(tmp_path: Path) -> None:
    """전 필드 라운드트립 동등성 — 2026-06-10 회귀.

    저장은 asdict 로 전 필드가 나가는데 복원(_from_json_dict)에서 turnover_ratio
    등 6개 필드가 빠져, 과매매 진단 지표가 주간 인사이트·대시보드에서 항상
    None/0 으로 떨어졌다. dataclass 동등성으로 향후 필드 추가 누락도 잡는다.
    """
    from dataclasses import asdict, replace

    report = DailyReport(
        date="2026-06-09",
        mode="day-trade",
        dry_run=False,
        discovery_chosen=["A"],
        discovery_blocked=[("C", "below")],
        market_gates=[MarketGateInfo(market="KOSPI", last=1.0, sma5=1.0, sma20=1.0, above=True)],
        buys=[TradeRecord(symbol="A", side="buy", quantity=10, price=100.0, simulated=False)],
        liquidations=[],
        error_count=3,
        note_ko="요약",
        realized_pnl=100.0,
        realized_pnl_pct=10.0,
        sells=[TradeRecord(symbol="A", side="sell", quantity=10, price=110.0, simulated=False)],
        excluded_etf_count=2,
        rank_candidates=["A", "B"],
        discovery_chosen_names={"A": "에이전자"},
        closing_cash=1_000_000,
        closing_equity=1_100_000,
        closing_total_pnl=100_000,
        closing_holdings=[
            ClosingHolding(
                symbol="A",
                name="에이전자",
                quantity=1,
                avg_price=100.0,
                current_price=101.0,
                market_value=101.0,
                pnl=1.0,
                pnl_pct=1.0,
            )
        ],
        total_fees=30.0,
        total_taxes=220.0,
        total_slippage=110.0,
        realized_pnl_after_cost=-260.0,
        timeline=[TimelineEvent(time_kst="09:01", kind="buy", text="매수")],
        # 종전 복원 누락 6필드 — 이 테스트의 핵심.
        traded_notional=2_100_000.0,
        turnover_ratio=0.21,
        orders_failed_count=1,
        liquidations_failed_count=2,
        balance_failure_count=1,
        fatal_count=4,
        # 2026-06-15 신규 — 동일 footgun 재발 방지.
        regime="high_vol",
        breadth_advancing_pct=0.78,
        breadth_mean_change_pct=2.62,
        breadth_passed=True,
        round_trips=[
            RoundTrip(
                symbol="A",
                name="에이전자",
                quantity=10,
                buy_price=100.0,
                sell_price=110.0,
                buy_time_kst="09:05",
                sell_time_kst="10:35",
                holding_minutes=90,
                pnl=100.0,
                pnl_pct=10.0,
                exit_reason="partial_take",
                mfe_pct=12.0,
                mae_pct=-3.0,
            )
        ],
        blocked_outcomes=[
            BlockedOutcome(
                symbol="C",
                name="씨전자",
                reason="kospi_below_sma",
                intraday_pct=8.0,
                day_change_pct=9.0,
            )
        ],
        # 2026-06-19 신규 — 동일 footgun 재발 방지.
        today_notional_used=9_993_580,
        reentry_blocked_count=174,
    )
    # 진입 피처도 포함해 라운드트립 동등성 검증.
    report = replace(
        report,
        buys=[
            TradeRecord(
                symbol="A",
                side="buy",
                quantity=10,
                price=100.0,
                simulated=False,
                time_kst="09:05",
                reason="돌파",
                change_pct_at_entry=3.5,
                rank_at_entry=7,
            )
        ],
    )
    loaded = load_daily_report(save_daily_report(report, root=tmp_path))
    assert asdict(loaded) == asdict(report)
    assert loaded.turnover_ratio == 0.21
    assert loaded.regime == "high_vol"
    assert loaded.round_trips[0].holding_minutes == 90
    assert loaded.round_trips[0].mfe_pct == 12.0
    assert loaded.blocked_outcomes[0].intraday_pct == 8.0
    assert loaded.buys[0].rank_at_entry == 7
    assert loaded.today_notional_used == 9_993_580
    assert loaded.reentry_blocked_count == 174


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


def test_buy_record_carries_reason_and_time_from_log() -> None:
    """매수 레코드가 Signal.reason(진입 사유)과 체결 시각(KST)을 흡수한다."""
    events = LogEvents(
        orders_simulated=[
            {
                "symbol": "A",
                "side": "buy",
                "qty": 10,
                "price": 100.0,
                "reason": "돌파 모멘텀",
                "timestamp": "2026-06-15T00:05:00Z",  # UTC 00:05 → KST 09:05
            }
        ],
    )
    report = build_daily_report(date="2026-06-15", events=events, snapshot=None)
    assert report.buys[0].reason == "돌파 모멘텀"
    assert report.buys[0].time_kst == "09:05"


def test_sell_record_reason_falls_back_to_guard_reason() -> None:
    """dry-run guard 매도는 guard_reason 키만 들고 있어 그것을 매도 사유로 흡수한다."""
    events = LogEvents(
        orders_simulated=[
            {"symbol": "A", "side": "sell", "qty": 5, "price": 95.0, "guard_reason": "stop_loss"},
        ],
    )
    report = build_daily_report(date="2026-06-15", events=events, snapshot=None)
    assert report.sells[0].reason == "stop_loss"


def test_round_trips_built_with_holding_minutes_and_exit_reason() -> None:
    """매수→매도 FIFO 매칭으로 왕복 레코드 1건이 손익·보유시간·사유와 함께 생성된다."""
    events = LogEvents(
        orders_simulated=[
            {
                "symbol": "A",
                "side": "buy",
                "qty": 10,
                "price": 100.0,
                "timestamp": "2026-06-15T00:05:00Z",  # 09:05 KST
            },
            {
                "symbol": "A",
                "side": "sell",
                "qty": 10,
                "price": 110.0,
                "guard_reason": "partial_take",
                "timestamp": "2026-06-15T01:35:00Z",  # 10:35 KST
            },
        ],
    )
    report = build_daily_report(date="2026-06-15", events=events, snapshot=None)
    assert len(report.round_trips) == 1
    rt = report.round_trips[0]
    assert rt.symbol == "A"
    assert rt.pnl == 100.0  # (110-100)*10
    assert rt.pnl_pct == pytest.approx(10.0)
    assert rt.buy_time_kst == "09:05"
    assert rt.sell_time_kst == "10:35"
    assert rt.holding_minutes == 90
    assert rt.exit_reason == "partial_take"
    # 합계 손익도 종전대로 채워진다.
    assert report.realized_pnl == 100.0


def test_round_trip_for_eod_liquidation_defaults_exit_reason() -> None:
    """EOD 청산은 reason 결손 시 exit_reason='eod' 로 폴백한다."""
    events = LogEvents(
        orders_simulated=[{"symbol": "A", "side": "buy", "qty": 10, "price": 100.0}],
        liquidations_simulated=[{"symbol": "A", "qty": 10, "sell_price": 105.0}],
    )
    report = build_daily_report(date="2026-06-15", events=events, snapshot=None)
    assert len(report.round_trips) == 1
    assert report.round_trips[0].exit_reason == "eod"
    assert report.round_trips[0].pnl == 50.0


def test_sell_reason_labeled_from_guard_log_on_real_path() -> None:
    """실거래 경로(order_accepted) 매도는 reason 결손 — position_guard 로그로 라벨링한다."""
    events = LogEvents(
        orders_accepted=[
            {"symbol": "A", "side": "buy", "qty": 10, "price": 100.0},
            {"symbol": "A", "side": "sell", "qty": 10, "price": 95.0},  # guard_reason 없음
        ],
        stop_losses=[{"symbol": "A", "price": 95.0, "drop_pct": 5.0}],
    )
    report = build_daily_report(date="2026-06-15", events=events, snapshot=None)
    assert report.sells[0].reason == "stop_loss"
    # 왕복 레코드의 exit_reason 도 같이 채워진다.
    assert report.round_trips[0].exit_reason == "stop_loss"


def test_regime_and_breadth_extracted_from_logs() -> None:
    """daytrade.regime / daytrade.breadth_gate 로그가 DailyReport 최상위로 끌어올려진다."""
    events = LogEvents(
        regime={"event": "daytrade.regime", "regime": "high_vol", "routed": True},
        breadth_gate={
            "event": "daytrade.breadth_gate",
            "passed": True,
            "advancing_pct": 0.788,
            "mean_change_pct": 2.62,
        },
    )
    report = build_daily_report(date="2026-06-15", events=events, snapshot=None)
    assert report.regime == "high_vol"
    assert report.breadth_advancing_pct == 0.788
    assert report.breadth_mean_change_pct == 2.62
    assert report.breadth_passed is True


def test_regime_breadth_none_when_logs_absent() -> None:
    """게이트·라우팅 비활성(로그 없음)이면 맥락 필드는 모두 None 으로 안전 폴백."""
    report = build_daily_report(date="2026-06-15", events=LogEvents(), snapshot=None)
    assert report.regime is None
    assert report.breadth_passed is None
    assert report.round_trips == []


def _write_topcap(tmp_path: Path, date: str, rows: list[dict]) -> None:
    import json

    lines = [json.dumps(r) for r in rows]
    (tmp_path / f"topcap-{date}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_snapshot_join_enriches_entry_features_and_mfe_mae(tmp_path: Path) -> None:
    """topcap 스냅샷 조인으로 매수 진입 피처 + 왕복 MFE/MAE 가 채워진다."""
    # 스냅샷 ts 는 KST 오프셋. 매수 timestamp 00:05Z → KST 09:05 와 매칭되게 09:xx 로 둔다.
    _write_topcap(
        tmp_path,
        "2026-06-15",
        [
            {
                "ts": "2026-06-15T09:05:00+09:00",
                "symbol": "A",
                "price": 100.0,
                "change_pct": 3.5,
                "rank": 7,
            },  # 09:05 진입 근처
            {
                "ts": "2026-06-15T09:35:00+09:00",
                "symbol": "A",
                "price": 130.0,
                "change_pct": 30.0,
                "rank": 4,
            },  # 보유구간 고점
            {
                "ts": "2026-06-15T09:50:00+09:00",
                "symbol": "A",
                "price": 90.0,
                "change_pct": -10.0,
                "rank": 9,
            },  # 보유구간 저점
        ],
    )
    events = LogEvents(
        orders_simulated=[
            {
                "symbol": "A",
                "side": "buy",
                "qty": 10,
                "price": 100.0,
                "timestamp": "2026-06-15T00:05:00Z",
            },  # → KST 09:05
            {
                "symbol": "A",
                "side": "sell",
                "qty": 10,
                "price": 120.0,
                "guard_reason": "partial_take",
                "timestamp": "2026-06-15T00:55:00Z",
            },  # → 09:55
        ],
    )
    report = build_daily_report(
        date="2026-06-15", events=events, snapshot=None, snapshot_root=tmp_path
    )
    # 진입 피처: 09:05 스냅샷(여기 ts 09:05 == 09:05) 근처
    buy = report.buys[0]
    assert buy.change_pct_at_entry == 3.5
    assert buy.rank_at_entry == 7
    # MFE/MAE: 보유구간(09:05~09:55) 고점130·저점90, 매수가100 기준
    rt = report.round_trips[0]
    assert rt.mfe_pct == pytest.approx(30.0)  # (130/100-1)*100
    assert rt.mae_pct == pytest.approx(-10.0)  # (90/100-1)*100


def test_blocked_outcomes_built_from_snapshot(tmp_path: Path) -> None:
    """차단 종목의 그날 시초→종가·전일대비 움직임이 반사실로 기록된다."""
    _write_topcap(
        tmp_path,
        "2026-06-15",
        [
            {
                "ts": "2026-06-15T00:01:00+09:00",
                "symbol": "C",
                "price": 100.0,
                "change_pct": 1.0,
                "rank": 50,
            },
            {
                "ts": "2026-06-15T06:30:00+09:00",
                "symbol": "C",
                "price": 108.0,
                "change_pct": 9.0,
                "rank": 30,
            },
        ],
    )
    events = LogEvents(
        discovery={
            "event": "daytrade.discovery",
            "chosen": [],
            "blocked": [["C", "kospi_below_sma"]],
        },
        # log_parser 가 discovery names 를 symbol_names 로 흡수하는 흐름을 모사.
        symbol_names={"C": "씨전자"},
    )
    report = build_daily_report(
        date="2026-06-15", events=events, snapshot=None, snapshot_root=tmp_path
    )
    assert len(report.blocked_outcomes) == 1
    bo = report.blocked_outcomes[0]
    assert bo.symbol == "C"
    assert bo.name == "씨전자"
    assert bo.reason == "kospi_below_sma"
    assert bo.intraday_pct == pytest.approx(8.0)  # 100 → 108
    assert bo.day_change_pct == 9.0


def test_snapshot_join_absent_file_leaves_none(tmp_path: Path) -> None:
    """스냅샷 파일 없으면 진입 피처·MFE/MAE·차단결과 모두 None/빈 — 과거 일자 호환."""
    events = LogEvents(
        orders_simulated=[
            {
                "symbol": "A",
                "side": "buy",
                "qty": 10,
                "price": 100.0,
                "timestamp": "2026-06-15T00:05:00Z",
            },
        ],
        discovery={"event": "daytrade.discovery", "chosen": [], "blocked": [["C", "x"]]},
    )
    report = build_daily_report(
        date="2026-06-15", events=events, snapshot=None, snapshot_root=tmp_path
    )
    assert report.buys[0].change_pct_at_entry is None
    assert report.buys[0].rank_at_entry is None
    # 차단 결과 레코드는 만들되 스냅샷 없으니 수치는 None
    assert report.blocked_outcomes[0].intraday_pct is None


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


def test_build_includes_blocked_symbol_names_in_name_map() -> None:
    """게이트 전종목 차단(chosen 비어있음) 이어도 blocked 코드 이름이 맵에 담긴다."""
    events = LogEvents(
        discovery={
            "chosen": [],
            "blocked": [["005930", "KOSPI_below_sma"], ["005250", "KOSPI_below_sma"]],
        },
        symbol_names={"005930": "삼성전자", "005250": "녹십자"},
    )
    report = build_daily_report(date="2026-05-18", events=events, snapshot=None)
    assert report.discovery_chosen_names == {"005930": "삼성전자", "005250": "녹십자"}


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


# 2026-05-16 회귀 — 장중 SELL(stop/trailing/partial) 이 리포트에서 통째로 버려져
# realized_pnl 이 항상 None 이던 버그. side=="sell" 체결을 sells 로 잡고 FIFO 로
# 라운드트립 손익을 산출해야 한다.


def test_intraday_sells_captured_and_realized_pnl_via_fifo() -> None:
    """실거래 accepted SELL(부분 익절+트레일링) → sells 채워지고 FIFO realized."""
    events = LogEvents(
        orders_accepted=[
            {"symbol": "403550", "side": "buy", "qty": 100, "price": 19_640.0},
            {"symbol": "403550", "side": "sell", "qty": 50, "price": 20_250.0},
            {"symbol": "403550", "side": "sell", "qty": 50, "price": 20_050.0},
        ],
    )
    report = build_daily_report(date="2026-05-14", events=events, snapshot=None)
    assert len(report.buys) == 1
    assert len(report.sells) == 2
    assert all(s.side == "sell" for s in report.sells)
    # FIFO: 50주 @+610, 50주 @+410 = 51,000원.
    expected = (20_250.0 - 19_640.0) * 50 + (20_050.0 - 19_640.0) * 50
    assert report.realized_pnl == expected
    assert report.realized_pnl_pct is not None
    assert "매도 2건" in report.note_ko


def test_sells_only_day_without_buys_has_no_realized_but_is_recorded() -> None:
    """전일 이월분만 매도한 날 — 매수가 모르면 손익 None, 그래도 sells 는 기록."""
    events = LogEvents(
        orders_accepted=[{"symbol": "005930", "side": "sell", "qty": 6, "price": 294_500.0}],
    )
    report = build_daily_report(date="2026-05-14", events=events, snapshot=None)
    assert len(report.sells) == 1
    assert report.realized_pnl is None  # 매수가 미상 → 추정 안 함
    assert "매도 1건" in report.note_ko


def test_sells_survive_json_roundtrip(tmp_path: Path) -> None:
    events = LogEvents(
        orders_accepted=[
            {"symbol": "A", "side": "buy", "qty": 10, "price": 100.0},
            {"symbol": "A", "side": "sell", "qty": 10, "price": 110.0},
        ],
    )
    report = build_daily_report(date="2026-05-14", events=events, snapshot=None)
    path = save_daily_report(report, root=tmp_path)
    loaded = load_daily_report(path)
    assert [s.symbol for s in loaded.sells] == ["A"]
    assert loaded.sells[0].side == "sell"
    assert loaded.realized_pnl == (110.0 - 100.0) * 10


# ---- 과매매 가시화: 거래대금·회전율·에러 분해 ----------------------------


def _snapshot_with_account(day_start: float) -> RunnerStateSnapshot:
    snap = _snapshot()
    return RunnerStateSnapshot(
        mode=snap.mode,
        dry_run=snap.dry_run,
        symbols=snap.symbols,
        strategies=snap.strategies,
        started_at=snap.started_at,
        snapshot=None,
        account=AccountState(deposit=0.0, total_equity=0.0, day_start_equity=day_start),
        recent_signals={},
        balance_failures=0,
        runner_mode=snap.runner_mode,
    )


def test_traded_notional_sums_priced_trades() -> None:
    events = LogEvents(
        orders_accepted=[
            {"symbol": "A", "side": "buy", "qty": 10, "price": 100.0},
            {"symbol": "A", "side": "sell", "qty": 10, "price": 110.0},
        ],
    )
    report = build_daily_report(date="2026-05-16", events=events, snapshot=None)
    assert report.traded_notional == 100.0 * 10 + 110.0 * 10  # 2100


def test_traded_notional_none_when_no_priced_trades() -> None:
    report = build_daily_report(date="2026-05-16", events=LogEvents(), snapshot=None)
    assert report.traded_notional is None
    assert report.turnover_ratio is None


def test_turnover_ratio_uses_day_start_equity() -> None:
    events = LogEvents(
        orders_accepted=[
            {"symbol": "A", "side": "buy", "qty": 10, "price": 100.0},
            {"symbol": "A", "side": "sell", "qty": 10, "price": 110.0},
        ],
    )
    report = build_daily_report(
        date="2026-05-16",
        events=events,
        snapshot=_snapshot_with_account(day_start=10_500.0),
    )
    assert report.traded_notional == 2100.0
    assert report.turnover_ratio == 2100.0 / 10_500.0  # 0.2배


def test_turnover_ratio_none_without_account() -> None:
    """분모 추정 금지 — day_start_equity 없으면 거래대금만, 회전율 None."""
    events = LogEvents(
        orders_accepted=[{"symbol": "A", "side": "buy", "qty": 10, "price": 100.0}],
    )
    report = build_daily_report(date="2026-05-16", events=events, snapshot=_snapshot())
    assert report.traded_notional == 1000.0
    assert report.turnover_ratio is None


def test_error_breakdown_fields_populated() -> None:
    events = LogEvents(
        orders_failed=[{"symbol": "A", "side": "buy", "qty": 1, "error": "x"}],
        liquidations_failed=[{"symbol": "B", "qty": 1, "error": "y"}],
        balance_failures=3,
        fatal=[{"consecutive": 5}],
    )
    report = build_daily_report(date="2026-05-16", events=events, snapshot=None)
    assert report.orders_failed_count == 1
    assert report.liquidations_failed_count == 1
    assert report.balance_failure_count == 1  # >0 → 1
    assert report.fatal_count == 1
    assert report.error_count == 4  # 합은 기존과 동일
