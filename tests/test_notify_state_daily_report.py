"""format_daily_report — payload 가 사용자에게 한국어로 잘 보이는지."""

from __future__ import annotations

from auto_trading.notify.state import format_daily_report
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


def test_format_omits_cost_line_when_equal_to_realized() -> None:
    """비용 차감치가 realized 와 같으면 노이즈라 라인 생략."""
    msg = format_daily_report(
        _payload(
            realized_pnl=10000.0,
            realized_pnl_pct=10.0,
            realized_pnl_after_cost=10000.0,
        )
    )
    assert "💸" not in msg


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
    assert "총평가: 50,032,500원" in msg
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
    assert "005930 삼성전자" in msg
    assert "+32,500원" in msg
    assert "+0.98%" in msg


def test_format_omits_closing_section_when_cash_missing() -> None:
    """closing_cash=None (수동 재생성 등) 이면 마감 잔고 섹션 자체가 안 뜬다."""
    msg = format_daily_report(_payload(closing_cash=None))
    assert "마감 잔고" not in msg
