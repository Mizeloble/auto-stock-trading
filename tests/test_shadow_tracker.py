"""Shadow Strategy Tracker — 29단계 PR42 (#7)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from auto_trading._common import KST, Side
from auto_trading.shadow.tracker import ShadowSignal, ShadowTracker


def _signal(
    *,
    symbol: str = "AAA",
    side: Side = Side.BUY,
    quantity: int = 10,
    price: float = 10_000.0,
    ts: datetime | None = None,
) -> ShadowSignal:
    return ShadowSignal(
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        ts=ts or datetime(2026, 5, 1, tzinfo=KST),
    )


# ---- 기본 흐름 ------------------------------------------------------------


def test_buy_reduces_cash_and_increases_holdings() -> None:
    t = ShadowTracker(initial_cash=1_000_000, slippage_bps=0.0)
    t.apply(_signal(symbol="X", side=Side.BUY, quantity=10, price=10_000))
    snap = t.snapshot()
    assert snap.holdings["X"] == 10
    # 10 x 10_000 = 100_000, fee 100_000 * 0.00015 = 15 → cash = 1_000_000 - 100_015
    assert snap.cash == pytest.approx(1_000_000 - 100_015, rel=1e-9)


def test_sell_increases_cash_and_realizes_pnl() -> None:
    t = ShadowTracker(
        initial_cash=1_000_000, slippage_bps=0.0, commission_rate=0.0, tax_rate_sell=0.0
    )
    t.apply(_signal(side=Side.BUY, quantity=10, price=10_000))
    t.apply(_signal(side=Side.SELL, quantity=10, price=12_000))
    snap = t.snapshot()
    # 매수 100_000 → 매도 120_000 → 실현 PnL +20_000
    assert snap.realized_pnl == pytest.approx(20_000.0)
    assert snap.holdings.get("AAA", 0) == 0


def test_sell_without_holdings_ignored() -> None:
    """라이브 환경에서 보유 없는 SELL → 무시 (안전)."""
    t = ShadowTracker(initial_cash=1_000_000)
    t.apply(_signal(side=Side.SELL, quantity=5, price=10_000))
    snap = t.snapshot()
    assert snap.realized_pnl == 0.0
    assert snap.num_signals == 0  # 무시된 시그널은 카운트 X


def test_fifo_matching_partial_sell() -> None:
    """매수 2 lot 후 부분 매도 — FIFO 로 첫 lot 부터."""
    t = ShadowTracker(
        initial_cash=10_000_000, slippage_bps=0.0, commission_rate=0.0, tax_rate_sell=0.0
    )
    t.apply(_signal(symbol="X", side=Side.BUY, quantity=10, price=10_000))
    t.apply(_signal(symbol="X", side=Side.BUY, quantity=10, price=12_000))
    # 15주 매도 — 첫 lot 10주 (10_000), 둘째 lot 5주 (12_000)
    t.apply(_signal(symbol="X", side=Side.SELL, quantity=15, price=15_000))
    # PnL = (15_000 - 10_000) * 10 + (15_000 - 12_000) * 5 = 50_000 + 15_000 = 65_000
    assert t.snapshot().realized_pnl == pytest.approx(65_000.0)


def test_slippage_applied_to_buy_and_sell() -> None:
    """slippage_bps > 0 → BUY 비싸게, SELL 싸게 체결."""
    t = ShadowTracker(
        initial_cash=1_000_000, slippage_bps=100.0, commission_rate=0.0, tax_rate_sell=0.0
    )
    # 100bp = 1% → BUY 가격 10_100, SELL 가격 9_900
    t.apply(_signal(side=Side.BUY, quantity=10, price=10_000))
    # 매수 비용 10 * 10_100 = 101_000
    assert t.cash == pytest.approx(1_000_000 - 101_000)


# ---- 시그널 카운트 ---------------------------------------------------------


def test_num_signals_increments() -> None:
    t = ShadowTracker(initial_cash=1_000_000)
    for _i in range(3):
        t.apply(_signal(symbol="X", side=Side.BUY, quantity=1, price=10_000))
    assert t.snapshot().num_signals == 3


# ---- equity ---------------------------------------------------------------


def test_equity_uses_marks_for_held_symbols() -> None:
    t = ShadowTracker(initial_cash=1_000_000, slippage_bps=0.0, commission_rate=0.0)
    t.apply(_signal(symbol="X", side=Side.BUY, quantity=10, price=10_000))
    # cash = 900_000. 평가 marks=12_000 → 보유 가치 120_000 → equity 1_020_000.
    assert t.equity({"X": 12_000}) == pytest.approx(1_020_000)


def test_equity_falls_back_to_avg_price_when_no_mark() -> None:
    t = ShadowTracker(initial_cash=1_000_000, slippage_bps=0.0, commission_rate=0.0)
    t.apply(_signal(symbol="X", side=Side.BUY, quantity=10, price=10_000))
    # marks 누락 → avg_price 사용 → 평가 100_000 → cash 900_000 → equity 1_000_000.
    assert t.equity({}) == pytest.approx(1_000_000)


# ---- jsonl 영속 -----------------------------------------------------------


def test_jsonl_log_appends_each_signal(tmp_path: Path) -> None:
    log = tmp_path / "shadow.jsonl"
    t = ShadowTracker(initial_cash=1_000_000, log_path=log)
    t.apply(_signal(side=Side.BUY, quantity=5, price=10_000))
    t.apply(_signal(side=Side.BUY, quantity=3, price=10_500))
    assert log.exists()
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2


def test_replay_yields_recorded_signals(tmp_path: Path) -> None:
    log = tmp_path / "shadow.jsonl"
    t = ShadowTracker(initial_cash=1_000_000, log_path=log)
    t.apply(_signal(side=Side.BUY, quantity=5, price=10_000, ts=datetime(2026, 5, 1, tzinfo=KST)))
    t.apply(_signal(side=Side.SELL, quantity=5, price=11_000, ts=datetime(2026, 5, 2, tzinfo=KST)))
    records = list(ShadowTracker.replay(log))
    assert len(records) == 2
    assert records[0]["side"] == "buy"
    assert records[1]["side"] == "sell"


def test_replay_missing_path_yields_nothing(tmp_path: Path) -> None:
    records = list(ShadowTracker.replay(tmp_path / "missing.jsonl"))
    assert records == []


# ---- 다중 심볼 -----------------------------------------------------------


def test_holds_multiple_symbols_independently() -> None:
    t = ShadowTracker(
        initial_cash=10_000_000, slippage_bps=0.0, commission_rate=0.0, tax_rate_sell=0.0
    )
    t.apply(_signal(symbol="A", side=Side.BUY, quantity=5, price=10_000))
    t.apply(_signal(symbol="B", side=Side.BUY, quantity=3, price=20_000))
    snap = t.snapshot()
    assert snap.holdings == {"A": 5, "B": 3}
    assert snap.avg_prices["A"] == pytest.approx(10_000)
    assert snap.avg_prices["B"] == pytest.approx(20_000)
