"""백테스트 실행 엔진.

흐름:
1. 과거 DataFrame (ts index, OHLCV) 을 순회
2. 각 bar 마다 전략 `on_bar()` 호출 → Signal 수집
3. **다음 bar 시가** 에 SimulatedBroker 로 체결 (look-ahead 방지)
4. 매 bar 마감 시점 에쿼티를 equity_curve 로 기록
5. 종료 후 metrics + 결과 디렉터리에 CSV 저장
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

import pandas as pd

from ..strategy.base import Bar, Side, Signal, Strategy
from .broker import SimulatedBroker, Trade
from .metrics import PerformanceReport, compute


@dataclass
class BacktestResult:
    report: PerformanceReport
    equity_curve: pd.Series
    trades: list[Trade]
    output_dir: Path | None = None


@dataclass
class Backtester:
    strategy: Strategy
    initial_cash: float = 10_000_000.0   # 기본 천만원
    position_sizing: str = "all_in"      # all_in: 가용 현금 전부로 매수
    commission_rate: float = 0.00015
    tax_rate_sell: float = 0.0018
    slippage_bps: float = 0.0

    _broker: SimulatedBroker = field(init=False)
    _equity: list[tuple[datetime, float]] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        self._broker = SimulatedBroker(
            cash=self.initial_cash,
            commission_rate=self.commission_rate,
            tax_rate_sell=self.tax_rate_sell,
            slippage_bps=self.slippage_bps,
        )

    def run(self, symbol: str, df: pd.DataFrame) -> BacktestResult:
        """`df` 는 ts 인덱스(Datetime) + columns [open, high, low, close, volume]."""
        self.strategy.reset()
        pending: list[Signal] = []

        rows = list(df.itertuples(index=True))
        for i, row in enumerate(rows):
            ts: datetime = row.Index  # type: ignore[assignment]
            bar = Bar(
                symbol=symbol,
                ts=ts,
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                volume=int(row.volume),
            )

            # 전 bar 에서 나온 시그널을 "오늘 시가"에 체결
            if pending:
                for sig in pending:
                    qty = self._size_quantity(sig, bar.open, symbol)
                    self._broker.execute(
                        symbol=sig.symbol,
                        side=sig.side,
                        quantity=qty,
                        bar_open=bar.open,
                        ts=ts,
                    )
                pending = []

            signals = self.strategy.on_bar(bar)
            if signals:
                pending.extend(signals)

            # 에쿼티 기록 (종가 기준)
            equity = self._broker.equity({symbol: bar.close})
            self._equity.append((ts, equity))

        # 마지막 bar 이후 잔여 시그널은 버린다 (체결할 다음 bar 없음)

        equity_curve = pd.Series(
            data=[e for _, e in self._equity],
            index=pd.DatetimeIndex([t for t, _ in self._equity], name="ts"),
            name="equity",
        )
        report = compute(equity_curve, self._broker.trades, self.initial_cash)
        return BacktestResult(report=report, equity_curve=equity_curve, trades=self._broker.trades)

    def _size_quantity(self, signal: Signal, price: float, symbol: str) -> int:
        if signal.side is Side.SELL:
            pos = self._broker.positions.get(symbol)
            return pos.quantity if pos else 0
        if price <= 0:
            return 0
        # all_in: 가용 현금으로 살 수 있는 최대 수량 * weight
        budget = self._broker.cash * max(0.0, min(1.0, signal.weight))
        qty = int(budget // (price * (1 + self.commission_rate)))
        return max(0, qty)


def save_result(result: BacktestResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)

    equity_path = out_dir / "equity.csv"
    result.equity_curve.to_csv(equity_path, header=True)

    trades_path = out_dir / "trades.csv"
    with trades_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["ts", "symbol", "side", "price", "quantity", "fee", "tax"])
        for t in result.trades:
            writer.writerow([
                t.ts.isoformat(), t.symbol, t.side.value,
                f"{t.price:.4f}", t.quantity, f"{t.fee:.4f}", f"{t.tax:.4f}",
            ])

    report_path = out_dir / "report.json"
    report_path.write_text(json.dumps(result.report.as_dict(), ensure_ascii=False, indent=2))
    result.output_dir = out_dir
    return out_dir


def frames_from_bars(bars: Iterable) -> pd.DataFrame:
    """market.chart.Bar 리스트(또는 OHLCV dict) → 백테스트용 DataFrame."""
    records = []
    for b in bars:
        if hasattr(b, "ts"):
            records.append({
                "ts": b.ts, "open": float(b.open), "high": float(b.high),
                "low": float(b.low), "close": float(b.close), "volume": int(b.volume),
            })
        else:
            records.append(b)
    df = pd.DataFrame(records)
    if df.empty:
        return df
    return df.sort_values("ts").set_index("ts")
