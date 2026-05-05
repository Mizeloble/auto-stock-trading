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


def test_format_skips_pnl_when_prices_missing() -> None:
    """sell_price 없는 시뮬 청산은 손익 라벨 없이 표기."""
    msg = format_daily_report(
        _payload(
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
