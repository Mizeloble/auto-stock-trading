"""_pair_buy_liq_records / _format_pair_line / _format_buy_line / _format_sell_line 단위 테스트.

format_daily_report 통합 테스트는 test_notify_state_daily_report.py 가 별도 커버.
여기서는 분해된 헬퍼 4종을 격리해서 페어링·포맷 분기마다 한 줄씩 검증한다.
"""

from __future__ import annotations

from auto_trading.notify.state import (
    _format_buy_line,
    _format_pair_line,
    _format_sell_line,
    _pair_buy_liq_records,
)


def test_pair_buy_liq_records_classifies_paired_only_buy_only_sell() -> None:
    """페어/매수만/청산만 3분류가 입력 순서대로 안정적이어야 함."""
    buys = [
        {"symbol": "A", "quantity": 10, "price": 100.0},
        {"symbol": "B", "quantity": 5, "price": 200.0},  # only-buy
    ]
    liqs = [
        {"symbol": "A", "quantity": 10, "sell_price": 110.0, "buy_price": 100.0},
        {"symbol": "C", "quantity": 3, "sell_price": 50.0, "buy_price": 60.0},  # only-sell
    ]
    paired, only_buys, only_sells = _pair_buy_liq_records(buys, liqs)
    assert len(paired) == 1
    assert paired[0][0]["symbol"] == "A"
    assert paired[0][1]["symbol"] == "A"
    assert [b["symbol"] for b in only_buys] == ["B"]
    assert [s["symbol"] for s in only_sells] == ["C"]


def test_pair_buy_liq_records_dedupe_first_buy_per_symbol() -> None:
    """한 종목 매수 여러 건이면 첫 건만 페어 베이스로 (day-trade 1:1 가정)."""
    buys = [
        {"symbol": "A", "quantity": 10, "price": 100.0},
        {"symbol": "A", "quantity": 5, "price": 105.0},  # 무시되어야 함
    ]
    liqs = [{"symbol": "A", "quantity": 15, "sell_price": 110.0, "buy_price": 100.0}]
    paired, only_buys, only_sells = _pair_buy_liq_records(buys, liqs)
    assert len(paired) == 1
    assert paired[0][0]["price"] == 100.0  # 첫 건만 채택
    assert only_buys == []
    assert only_sells == []


def test_format_pair_line_includes_pnl_and_simulated_marker() -> None:
    """페어 라인 — 손익 양수 + 시뮬 표시."""
    buy = {"symbol": "018880", "name": "한진", "quantity": 10, "price": 5000.0, "simulated": True}
    liq = {"symbol": "018880", "quantity": 10, "sell_price": 5500.0, "buy_price": 5000.0}
    line = _format_pair_line(buy, liq)
    assert "018880 한진" in line
    assert "10주" in line
    assert "@5,000원" in line and "@5,500원" in line
    assert "+5,000원/+10.00%" in line
    assert "(시뮬)" in line


def test_format_pair_line_market_price_no_pnl() -> None:
    """price 없으면 @시장가 + 손익 표기 생략."""
    buy = {"symbol": "A", "quantity": 5, "price": None}
    liq = {"symbol": "A", "quantity": 5, "sell_price": None, "buy_price": None}
    line = _format_pair_line(buy, liq)
    assert "@시장가" in line
    assert "+" not in line  # 손익 부호 없음
    assert "(시뮬)" not in line


def test_format_buy_line_renders_qty_price_simulated() -> None:
    buy = {"symbol": "005930", "quantity": 13, "price": 254000.0, "simulated": True}
    line = _format_buy_line(buy)
    assert line == "- 005930 BUY 13주 @254,000원 (시뮬)"


def test_format_sell_line_renders_pnl_when_prices_present() -> None:
    """청산만 라인 — 손익 음수 표기."""
    liq = {
        "symbol": "A",
        "name": "테스트",
        "quantity": 10,
        "sell_price": 90.0,
        "buy_price": 100.0,
        "simulated": False,
    }
    line = _format_sell_line(liq)
    assert "A 테스트" in line
    assert "SELL 10주 @90원" in line
    assert "(-100원/-10.00%)" in line


def test_format_sell_line_omits_pnl_when_buy_price_missing() -> None:
    liq = {"symbol": "A", "quantity": 10, "sell_price": 90.0, "buy_price": None}
    line = _format_sell_line(liq)
    assert "@90원" in line
    assert "원/" not in line  # 손익 표기 없음
