"""_common 헬퍼 contract 테스트."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from auto_trading._common import (
    KST,
    Bar,
    Side,
    extract_rows,
    parse_kiwoom_ts,
    to_decimal,
)


def test_side_enum_has_buy_and_sell() -> None:
    assert Side.BUY.value == "buy"
    assert Side.SELL.value == "sell"


def test_bar_is_hashable_and_immutable() -> None:
    bar = Bar(symbol="X", ts=datetime(2025, 1, 1, tzinfo=KST), open=1, high=2, low=0.5, close=1.5, volume=10)
    assert hash(bar)
    assert bar.symbol == "X"


def test_to_decimal_strips_commas_and_plus() -> None:
    assert to_decimal("+1,234,567") == Decimal(1234567)
    assert to_decimal("-500") == Decimal(-500)
    assert to_decimal("70000") == Decimal(70000)


def test_to_decimal_returns_zero_on_junk() -> None:
    assert to_decimal(None) == Decimal(0)
    assert to_decimal("") == Decimal(0)
    assert to_decimal("-") == Decimal(0)
    assert to_decimal("abc") == Decimal(0)


def test_parse_kiwoom_ts_handles_common_formats() -> None:
    assert parse_kiwoom_ts("20250102").date() == datetime(2025, 1, 2).date()
    assert parse_kiwoom_ts("20250102153045").hour == 15
    # HHMMSS uses today's date
    hhmmss = parse_kiwoom_ts("093000")
    assert hhmmss.hour == 9 and hhmmss.minute == 30
    # 잘못된 포맷은 now 로 폴백 (raise 하지 않음)
    assert isinstance(parse_kiwoom_ts(""), datetime)
    assert isinstance(parse_kiwoom_ts("garbage"), datetime)


def test_extract_rows_finds_first_list_of_dicts() -> None:
    payload = {
        "_cont_yn": "N",
        "meta": "x",
        "rows": [{"a": 1}, {"a": 2}],
        "other": [{"b": 3}],
    }
    assert extract_rows(payload) == [{"a": 1}, {"a": 2}]


def test_extract_rows_empty_when_missing() -> None:
    assert extract_rows({"_next_key": "", "scalar": 42}) == []
