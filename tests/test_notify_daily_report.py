"""format_daily_report — payload 가 사용자에게 한국어로 잘 보이는지."""

from __future__ import annotations

from auto_trading.notify.daily_report import format_daily_report
from auto_trading.notify.telegram import _format_message


def _payload(**overrides: object) -> dict:
    base: dict = {
        "date": "2026-05-05",
        "mode": "day-trade",
        "dry_run": True,
        "discovery_chosen": ["018880", "010170"],
        "discovery_blocked": [["001510", "kospi_below_sma"]],
        "market_gates": [
            {"market": "KOSPI", "last": 6936.99, "sma5": 6696.56, "sma20": 6209.99, "above": True},
            {"market": "KOSDAQ", "last": 1213.74, "sma5": 1213.62, "sma20": 1151.6, "above": True},
        ],
        "buys": [
            {"symbol": "018880", "side": "buy", "quantity": 10, "price": 5000.0, "simulated": True}
        ],
        "liquidations": [
            {
                "symbol": "018880",
                "quantity": 10,
                "sell_price": 5500.0,
                "buy_price": 5000.0,
                "simulated": True,
            }
        ],
        "error_count": 0,
        "note_ko": "매수 1건 · 청산 1건",
        "realized_pnl": 5000.0,
        "realized_pnl_pct": 10.0,
        "excluded_etf_count": 53,
        "rank_candidates": ["018880", "010170"],
    }
    base.update(overrides)
    return base


def test_format_includes_date_mode_and_summary_lines() -> None:
    msg = format_daily_report(_payload())
    assert "📊 일일 리포트 (2026-05-05)" in msg
    assert "day-trade" in msg
    assert "dry-run" in msg
    assert "018880" in msg
    assert "kospi_below_sma" in msg
    assert "ETF 53건 제외" in msg
    assert "+5,000원" in msg or "+5,000" in msg
    assert "+10.00%" in msg


def test_format_shows_market_context_regime_and_breadth() -> None:
    """국면·시장폭이 초보 친화 한국어로 노출된다."""
    msg = format_daily_report(
        _payload(
            regime="high_vol",
            breadth_advancing_pct=0.788,
            breadth_mean_change_pct=2.62,
            breadth_passed=True,
        )
    )
    assert "🧭" in msg
    assert "고변동성" in msg  # high_vol → 한국어
    assert "high_vol" not in msg  # 코드값은 노출 안 함
    assert "시장폭 상승 79%" in msg
    assert "통과" in msg


def test_format_shows_no_trade_reason_when_no_buys() -> None:
    """매수 0건이면 미진입 사유가 한 줄로 노출된다."""
    msg = format_daily_report(
        _payload(
            buys=[],
            liquidations=[],
            realized_pnl=None,
            note_ko="거래 없음.",
            no_trade_reason="시장 폭 게이트 차단 — 상승 종목이 부족해 오늘은 진입하지 않았어요.",
        )
    )
    assert "🚫" in msg
    assert "상승 종목이 부족" in msg


def test_format_minimal_no_buys_no_liqs() -> None:
    msg = format_daily_report(
        _payload(
            buys=[],
            liquidations=[],
            realized_pnl=None,
            realized_pnl_pct=None,
            error_count=0,
            note_ko="거래 없음.",
        )
    )
    assert "💰 매수" not in msg
    assert "🧹 청산" not in msg
    assert "거래 없음" in msg


def test_format_message_routes_daily_report_event() -> None:
    """telegram._format_message 가 runner.daily_report 이벤트를 format_daily_report 로 위임."""
    msg = _format_message("runner.daily_report", _payload())
    assert "📊 일일 리포트" in msg


def test_format_handles_many_buys_with_truncation() -> None:
    """매수 7건 이상이면 첫 6건 + '외 N건'."""
    many = [
        {"symbol": f"00{i:04d}", "side": "buy", "quantity": 1, "price": 100.0, "simulated": True}
        for i in range(8)
    ]
    msg = format_daily_report(_payload(buys=many))
    assert "외 2건" in msg


def test_format_includes_cost_adjusted_pnl_when_present() -> None:
    """23단계 Phase A — realized_pnl_after_cost 가 있고 realized_pnl 과 다르면 별도 라인."""
    msg = format_daily_report(
        _payload(
            realized_pnl=10000.0,
            realized_pnl_pct=10.0,
            realized_pnl_after_cost=9650.0,
        )
    )
    assert "💸 비용 차감 PnL" in msg
    assert "+9,650원" in msg


def test_format_always_shows_cost_line_even_when_equal() -> None:
    """비용 0이라 값이 같아도 초보에게 '수수료·세금 반영됨' 을 명시하려 항상 노출.

    이전엔 같으면 생략했으나, 비용 개념이 낯선 사용자가 실현=총이익으로 오해 → 항상 2줄.
    """
    msg = format_daily_report(
        _payload(
            realized_pnl=10000.0,
            realized_pnl_pct=10.0,
            realized_pnl_after_cost=10000.0,
        )
    )
    assert "💸 비용 차감 PnL" in msg
    assert "+10,000원" in msg


def test_format_shows_traded_notional_and_turnover() -> None:
    """과매매 가시화 — 거래대금·회전율이 요약 줄에 노출."""
    msg = format_daily_report(_payload(traded_notional=1_500_000.0, turnover_ratio=3.2))
    assert "거래대금 1,500,000원" in msg
    assert "회전 3.2배" in msg


def test_format_error_breakdown_replaces_lump_count() -> None:
    """에러 통짜 대신 유형별 분해(0 인 항목 생략)."""
    msg = format_daily_report(
        _payload(
            error_count=3,
            orders_failed_count=2,
            liquidations_failed_count=0,
            balance_failure_count=1,
            fatal_count=0,
        )
    )
    assert "에러 주문 2 · 잔고 1" in msg
    assert "에러 3건" not in msg


def test_format_skips_pnl_when_prices_missing() -> None:
    """sell_price 없는 시뮬 청산은 손익 라벨 없이 표기."""
    msg = format_daily_report(
        _payload(
            buys=[],  # 페어 분기를 회피해 only-sell 케이스로 강제.
            liquidations=[
                {
                    "symbol": "A",
                    "quantity": 10,
                    "sell_price": None,
                    "buy_price": None,
                    "simulated": True,
                }
            ],
            realized_pnl=None,
            realized_pnl_pct=None,
        )
    )
    # 청산 라인은 있으나 (+/-) 손익 표기는 없음.
    assert "🧹 청산" in msg
    assert "(+" not in msg.split("🧹 청산")[1].split("📋")[0]


def test_format_renders_chosen_names_next_to_codes() -> None:
    """선정 라인에 종목명이 있으면 코드 옆에 붙는다."""
    msg = format_daily_report(
        _payload(
            discovery_chosen_names={"018880": "한진", "010170": "현대건설"},
        )
    )
    assert "선정: 018880 한진, 010170 현대건설" in msg


def test_format_renders_blocked_names_next_to_codes() -> None:
    """차단 라인도 이름이 있으면 코드 옆에 붙는다 (게이트 전종목 차단 케이스)."""
    msg = format_daily_report(
        _payload(
            discovery_chosen=[],
            discovery_blocked=[
                ["005930", "KOSPI_below_sma"],
                ["005250", "KOSPI_below_sma"],
            ],
            discovery_chosen_names={"005930": "삼성전자", "005250": "녹십자"},
        )
    )
    assert "차단: 005930 삼성전자(KOSPI_below_sma), 005250 녹십자(KOSPI_below_sma)" in msg


def test_format_renders_pair_view_when_buy_and_liquidation_match() -> None:
    """같은 종목에 buy + liquidation 있으면 한 줄 페어 렌더로."""
    msg = format_daily_report(
        _payload(
            discovery_chosen_names={"018880": "한진"},
            buys=[
                {
                    "symbol": "018880",
                    "side": "buy",
                    "quantity": 10,
                    "price": 5000.0,
                    "simulated": False,
                    "name": "한진",
                }
            ],
            liquidations=[
                {
                    "symbol": "018880",
                    "quantity": 10,
                    "sell_price": 5500.0,
                    "buy_price": 5000.0,
                    "simulated": False,
                    "name": "한진",
                }
            ],
        )
    )
    assert "📋 매매 결과" in msg
    assert "018880 한진" in msg
    assert "@5,000원" in msg
    assert "@5,500원" in msg
    assert "+5,000원" in msg and "+10.00%" in msg
    # 페어로 갔으니 별도 매수/청산 섹션은 안 뜬다.
    assert "💰 매수:" not in msg
    assert "🧹 청산:" not in msg


def test_format_includes_closing_balance_when_present() -> None:
    msg = format_daily_report(
        _payload(
            closing_cash=50_000_000,
            closing_equity=50_032_500,
            closing_holdings=[],
        )
    )
    assert "💰 마감 잔고" in msg
    assert "예수금: 50,000,000원" in msg
    assert "보유 평가금: 50,032,500원" in msg
    # 계좌 총합(= cash + equity) 한 줄도 함께 노출.
    assert "계좌 총합: 100,032,500원" in msg
    assert "보유: 없음 (전량 청산)" in msg


def test_format_closing_holdings_render_with_pnl() -> None:
    msg = format_daily_report(
        _payload(
            closing_cash=46_700_000,
            closing_equity=50_034_500,
            closing_holdings=[
                {
                    "symbol": "005930",
                    "name": "삼성전자",
                    "quantity": 13,
                    "avg_price": 254000.0,
                    "current_price": 256500.0,
                    "market_value": 3334500.0,
                    "pnl": 32500.0,
                    "pnl_pct": 0.98,
                }
            ],
        )
    )
    assert "보유: 1종목" in msg
    # 손익 라벨이 매수가 대비임을 명시 — 2026-05-11 사용자 피드백.
    assert "매수가 대비" in msg
    assert "005930 삼성전자" in msg
    assert "+32,500원" in msg
    assert "+0.98%" in msg


# 2026-05-11 사용자 피드백 — 시계열 섹션 + 라벨 명확화.


def test_format_renders_timeline_section_with_kst_time_prefix() -> None:
    """timeline 이벤트들이 [HH:MM] 시각 + 종류별 이모지 + 텍스트로 렌더."""
    msg = format_daily_report(
        _payload(
            timeline=[
                {
                    "time_kst": "08:55",
                    "kind": "discovery",
                    "text": "발굴 채택 3건: 005930 삼성전자",
                },
                {
                    "time_kst": "09:00",
                    "kind": "buy",
                    "text": "매수 005930 삼성전자 12주 @275,500원",
                },
                {
                    "time_kst": "09:01",
                    "kind": "partial_take",
                    "text": "부분 익절 005930 삼성전자 6주 @284,500원 +3.27%",
                },
                {
                    "time_kst": "09:01",
                    "kind": "kill_switch",
                    "text": "🛑 일일 손실 한도 도달 (당일 -3.50%, 한도 -3.0%) — 강제 청산",
                },
            ],
        )
    )
    assert "⏱ 오늘 흐름 (4건)" in msg
    assert "[08:55] 🔎 발굴 채택 3건" in msg
    assert "[09:00] 💰 매수 005930 삼성전자 12주 @275,500원" in msg
    assert "[09:01] 💵 부분 익절 005930 삼성전자 6주 @284,500원 +3.27%" in msg
    # kill switch 의 🛑 가 text 첫 글자라 prefix 중복되지 않게.
    assert "[09:01] 🛑 일일 손실 한도 도달" in msg


def test_format_timeline_section_omitted_when_empty() -> None:
    msg = format_daily_report(_payload(timeline=[]))
    assert "⏱ 오늘 흐름" not in msg


def test_format_timeline_truncates_with_overflow_note() -> None:
    timeline = [
        {"time_kst": f"09:{i:02d}", "kind": "buy", "text": f"매수 005930 {i}주"} for i in range(15)
    ]
    msg = format_daily_report(_payload(timeline=timeline))
    assert "외 3건" in msg  # 기본 max_lines=12 → 3건 잘림


def test_format_timeline_preserves_kill_switch_and_liquidate_past_truncate() -> None:
    """29단계 PR27 — 5/12 회귀: 13:15·14:51 손절·청산이 '외 N건' 으로 묻혀 사용자가
    그날 결말을 못 보던 동작 차단. truncate 안쪽이라도 kill_switch/liquidate 는 항상 포함.
    """
    timeline: list[dict] = [
        {"time_kst": f"09:{i:02d}", "kind": "buy", "text": f"매수 005930 {i}주"} for i in range(12)
    ]
    timeline.append(
        {
            "time_kst": "13:15",
            "kind": "kill_switch",
            "text": "🛑 일일 손실 한도 도달 (-3.0%) — 강제 청산",
        }
    )
    timeline.append({"time_kst": "13:30", "kind": "buy", "text": "매수 005930 99주"})
    timeline.append(
        {
            "time_kst": "15:15",
            "kind": "liquidate",
            "text": "🧹 청산 005930 11주 @시장가",
        }
    )
    msg = format_daily_report(_payload(timeline=timeline))
    # 잘려도 우선순위 이벤트는 등장
    assert "🛑 일일 손실 한도 도달" in msg
    assert "🧹 청산 005930" in msg
    # 우선순위 아닌 13:30 buy 는 "외 N건" 으로 묶임 (15 - 12 - 2 preserved = 1)
    assert "외 1건" in msg
    assert "매수 005930 99주" not in msg


def test_format_daily_report_counts_buys_for_repeat_entries() -> None:
    """5/12 회귀 — 같은 종목 다회 매수에서 페어 카운트가 1건으로 줄어 사용자 혼란.

    PR27: _pair_buy_liq_records 가 raw FIFO 페어링이라 N:M 매수/청산 케이스에서
    페어 수와 only_buy 수가 입력 그대로 보존.
    """
    buys = [{"symbol": "A", "quantity": 10, "price": 100.0, "simulated": True} for _ in range(3)]
    liqs = [
        {"symbol": "A", "quantity": 10, "sell_price": 110.0, "buy_price": 100.0, "simulated": True}
        for _ in range(2)
    ]
    msg = format_daily_report(_payload(buys=buys, liquidations=liqs))
    assert "📋 매매 결과: 2건" in msg  # FIFO 페어 2건
    assert "💰 매수: 1건" in msg  # 페어 안 된 잉여 매수 1건


def test_format_summary_line_splits_buy_and_liq_counts() -> None:
    """기존 '매매 N건' 합산 표기가 헷갈린다는 피드백 → 매수/청산 분리."""
    msg = format_daily_report(_payload())
    # 매수 1 + 청산 1 케이스.
    assert "매수 1건 · 청산 1건" in msg
    # 합산 라벨은 사라져야 함.
    assert "매매 2건" not in msg


def test_format_omits_closing_section_when_cash_missing() -> None:
    """closing_cash=None (수동 재생성 등) 이면 마감 잔고 섹션 자체가 안 뜬다."""
    msg = format_daily_report(_payload(closing_cash=None))
    assert "마감 잔고" not in msg
