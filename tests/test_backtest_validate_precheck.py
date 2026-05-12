"""백테스트 엔진의 OHLCV 정합성 pre-check — 29단계 PR30 (#19 후속).

PR29 의 validate_bars 를 Backtester.run() 입구에서 호출 → issue 발견 시 warning 로그.
백테스트 진행은 막지 않음 (결정론 보존).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import structlog

from auto_trading.backtest.engine import Backtester
from auto_trading.strategy.base import Bar, Signal, Strategy


class NoopStrategy(Strategy):
    def on_bar(self, _bar: Bar) -> list[Signal]:
        return []


def _df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows).set_index("ts")


def _clean_rows(n: int = 5) -> list[dict]:
    return [
        {
            "ts": datetime(2026, 5, 1) + timedelta(days=i),
            "open": 100.0 + i,
            "high": 105.0 + i,
            "low": 95.0 + i,
            "close": 102.0 + i,
            "volume": 1_000,
        }
        for i in range(n)
    ]


def _warnings_for(cap: list[dict]) -> list[dict]:
    return [c for c in cap if c.get("event") == "backtest.data_integrity_issues"]


def test_precheck_silent_on_clean_data() -> None:
    """정상 데이터 → warning 로그 0건."""
    df = _df(_clean_rows())
    bt = Backtester(strategy=NoopStrategy(), initial_cash=100_000.0)
    with structlog.testing.capture_logs() as cap:
        bt.run("TEST", df)
    assert _warnings_for(cap) == []


def test_precheck_warns_on_non_positive_price() -> None:
    """open=0 인 깨진 봉 주입 → warning 1회 + samples 포함."""
    rows = _clean_rows()
    rows[1]["open"] = 0.0
    df = _df(rows)
    bt = Backtester(strategy=NoopStrategy(), initial_cash=100_000.0)
    with structlog.testing.capture_logs() as cap:
        bt.run("TEST", df)
    warnings = _warnings_for(cap)
    assert len(warnings) == 1
    w = warnings[0]
    assert w["log_level"] == "warning"
    assert w["symbol"] == "TEST"
    assert w["count"] >= 1
    # OHLC 일관성 위반도 같이 보고됨 (open=0, high=106, low=96 → OHLC 가 broken).
    kinds = {sample[0] for sample in w["samples"]}
    assert "non_positive_price" in kinds


def test_precheck_warns_on_abnormal_gap() -> None:
    """+100% 갭 → abnormal_gap warning."""
    rows = _clean_rows(3)
    rows[2]["close"] = rows[1]["close"] * 5  # +400% 갭
    rows[2]["high"] = rows[2]["close"]
    rows[2]["open"] = rows[2]["close"]
    rows[2]["low"] = rows[2]["close"]
    df = _df(rows)
    bt = Backtester(strategy=NoopStrategy(), initial_cash=100_000.0)
    with structlog.testing.capture_logs() as cap:
        bt.run("TEST", df)
    warnings = _warnings_for(cap)
    assert warnings, "갭 1건이라도 warning 한 번은 떠야 한다"
    kinds = {sample[0] for sample in warnings[0]["samples"]}
    assert "abnormal_gap" in kinds


def test_precheck_does_not_block_execution() -> None:
    """깨진 데이터라도 백테스트 자체는 결정론적으로 진행 (warning 만)."""
    rows = _clean_rows()
    rows[2]["volume"] = -50  # 음수 거래량
    df = _df(rows)
    bt = Backtester(strategy=NoopStrategy(), initial_cash=100_000.0)
    result = bt.run("TEST", df)
    # 결과 객체 생성 + equity_curve 가 길이대로 채워짐.
    assert len(result.equity_curve) == len(rows)


def test_precheck_samples_truncated_to_five() -> None:
    """issue 가 많아도 samples 는 최대 5건 (로그 폭주 방지)."""
    # 다섯 봉 모두 OHLC 불일치 + 음수 거래량 + 비양수 가격 — issue 10+ 건.
    rows = [
        {
            "ts": datetime(2026, 5, 1) + timedelta(days=i),
            "open": -1.0,
            "high": -2.0,
            "low": -3.0,
            "close": -4.0,
            "volume": -1,
        }
        for i in range(6)
    ]
    df = _df(rows)
    bt = Backtester(strategy=NoopStrategy(), initial_cash=100_000.0)
    with structlog.testing.capture_logs() as cap:
        bt.run("TEST", df)
    warnings = _warnings_for(cap)
    assert len(warnings) == 1
    w = warnings[0]
    assert w["count"] > 5
    assert len(w["samples"]) == 5
