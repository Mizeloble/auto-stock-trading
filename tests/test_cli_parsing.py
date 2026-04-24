"""CLI 의 종목별 전략 파라미터 파서 검증."""

from __future__ import annotations

import pytest

from auto_trading.cli import _coerce_param, _parse_symbol_spec


def test_coerce_param_int() -> None:
    assert _coerce_param("3") == 3
    assert isinstance(_coerce_param("3"), int)


def test_coerce_param_float() -> None:
    assert _coerce_param("1.5") == 1.5
    assert isinstance(_coerce_param("1.5"), float)


def test_coerce_param_string() -> None:
    assert _coerce_param("all_in") == "all_in"


def test_parse_symbol_without_params() -> None:
    sym, params = _parse_symbol_spec("005930")
    assert sym == "005930"
    assert params == {}


def test_parse_symbol_with_params() -> None:
    sym, params = _parse_symbol_spec("005930:short=3:long=15")
    assert sym == "005930"
    assert params == {"short": 3, "long": 15}


def test_parse_symbol_rejects_malformed_param() -> None:
    with pytest.raises(Exception, match="key=value"):
        _parse_symbol_spec("005930:foo")
