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
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd
import structlog

from ..market.validate import validate_bars
from ..strategy import indicators as ind
from ..strategy.base import Bar, Side, Signal, Strategy
from .broker import SimulatedBroker, Trade
from .metrics import PerformanceReport, compute

log = structlog.get_logger(__name__)


@dataclass
class BacktestResult:
    report: PerformanceReport
    equity_curve: pd.Series
    trades: list[Trade]
    output_dir: Path | None = None


@dataclass
class Backtester:
    strategy: Strategy
    initial_cash: float = 10_000_000.0  # 기본 천만원
    # all_in: 가용 현금 * weight 로 최대 수량 매수.
    # atr_normalized: (initial_cash * atr_risk_pct) / ATR 로 수량 정규화 — 변동성
    # 큰 종목엔 자동으로 적게 들어감 (29단계 PR21 #6).
    position_sizing: str = "all_in"
    atr_period: int = 14
    atr_risk_pct: float = 0.01  # 자본 대비 단위 위험 (atr_normalized 활성 시만)
    commission_rate: float = 0.00015
    tax_rate_sell: float = 0.0020  # 2026 인상 반영
    slippage_bps: float = 5.0  # 0.05% 보수적 기본
    # 29단계 PR35 (#4) — 시장 충격 슬리피지. "fixed" (기본) / "impact".
    slippage_model: str = "fixed"
    impact_coefficient: float = 2.0

    _broker: SimulatedBroker = field(init=False)
    _equity: list[tuple[datetime, float]] = field(init=False, default_factory=list)
    # 29단계 PR21 — atr_normalized sizing 위한 직전 봉 누적. SELL 영향 X (수량은 보유 전체).
    _highs: list[float] = field(init=False, default_factory=list)
    _lows: list[float] = field(init=False, default_factory=list)
    _closes: list[float] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        self._broker = SimulatedBroker(
            cash=self.initial_cash,
            commission_rate=self.commission_rate,
            tax_rate_sell=self.tax_rate_sell,
            slippage_bps=self.slippage_bps,
            slippage_model=self.slippage_model,
            impact_coefficient=self.impact_coefficient,
        )

    def run(self, symbol: str, df: pd.DataFrame) -> BacktestResult:
        """`df` 는 ts 인덱스(Datetime) + columns [open, high, low, close, volume]."""
        self.strategy.reset()
        pending: list[Signal] = []

        # 29단계 PR30 — 입력 OHLCV 정합성 1회 체크. issue 발견 시 warning 로그.
        # 백테스트 진행 자체는 막지 않음 (사용자가 의도적으로 깨진 데이터로 시뮬할 수도
        # 있고, 백테스트 결과는 결정적이어야 함). strict 모드는 후속 PR.
        bars: list[Bar] = [
            Bar(
                symbol=symbol,
                ts=row.Index,
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                volume=int(row.volume),
            )
            for row in df.itertuples(index=True)
        ]
        issues = validate_bars(bars, symbol=symbol)
        if issues:
            log.warning(
                "backtest.data_integrity_issues",
                symbol=symbol,
                count=len(issues),
                samples=[(i.kind.value, i.index, i.detail) for i in issues[:5]],
            )

        for bar in bars:
            ts: datetime = bar.ts

            # 전 bar 에서 나온 시그널을 "오늘 시가"에 체결
            if pending:
                for sig in pending:
                    qty = self._size_quantity(sig, bar.open, symbol)
                    self._broker.execute(
                        symbol=sig.symbol,
                        side=sig.side,
                        quantity=qty,
                        bar_open=bar.open,
                        bar_volume=bar.volume,
                        ts=ts,
                    )
                pending = []

            # ATR sizing 용 history 누적 — strategy.on_bar 이후라도 OK
            # (다음 봉 시가에 체결되는 사이즈 계산에만 사용).
            self._highs.append(bar.high)
            self._lows.append(bar.low)
            self._closes.append(bar.close)

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
        weight = max(0.0, min(1.0, signal.weight))

        if self.position_sizing == "atr_normalized":
            # ATR 기반 변동성 정규화. period+1 봉 미달이면 진입 X (안전).
            atr_val = ind.atr(self._highs, self._lows, self._closes, period=self.atr_period)
            if atr_val is None or atr_val <= 0:
                return 0
            # 자본 * 단위 위험 / ATR = "1 ATR 움직임당 risk_pct% 손실" 수량.
            risk_budget = self.initial_cash * self.atr_risk_pct * weight
            qty_by_risk = risk_budget / atr_val
            # 현금 한도도 동시 적용 — 적은 쪽 채택.
            qty_by_cash = self._broker.cash / (price * (1 + self.commission_rate))
            return max(0, int(min(qty_by_risk, qty_by_cash)))

        # all_in: 가용 현금으로 살 수 있는 최대 수량 * weight
        budget = self._broker.cash * weight
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
            writer.writerow(
                [
                    t.ts.isoformat(),
                    t.symbol,
                    t.side.value,
                    f"{t.price:.4f}",
                    t.quantity,
                    f"{t.fee:.4f}",
                    f"{t.tax:.4f}",
                ]
            )

    report_path = out_dir / "report.json"
    report_path.write_text(json.dumps(result.report.as_dict(), ensure_ascii=False, indent=2))
    result.output_dir = out_dir
    return out_dir


def frames_from_bars(bars: Iterable) -> pd.DataFrame:
    """market.chart.Bar 리스트(또는 OHLCV dict) → 백테스트용 DataFrame."""
    records = []
    for b in bars:
        if hasattr(b, "ts"):
            records.append(
                {
                    "ts": b.ts,
                    "open": float(b.open),
                    "high": float(b.high),
                    "low": float(b.low),
                    "close": float(b.close),
                    "volume": int(b.volume),
                }
            )
        else:
            records.append(b)
    df = pd.DataFrame(records)
    if df.empty:
        return df
    return df.sort_values("ts").set_index("ts")
