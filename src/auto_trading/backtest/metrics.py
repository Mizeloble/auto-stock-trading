"""백테스트 성과 지표."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .broker import Trade
from ..strategy.base import Side


@dataclass
class PerformanceReport:
    initial_equity: float
    final_equity: float
    total_return: float     # 0.10 = +10%
    cagr: float
    annualized_vol: float
    sharpe: float           # 연환산
    max_drawdown: float     # 음수 (-0.15 = -15%)
    win_rate: float         # 0~1
    num_trades: int
    num_round_trips: int

    def as_dict(self) -> dict:
        return {
            "initial_equity": round(self.initial_equity, 2),
            "final_equity": round(self.final_equity, 2),
            "total_return_pct": round(self.total_return * 100, 2),
            "cagr_pct": round(self.cagr * 100, 2),
            "annualized_vol_pct": round(self.annualized_vol * 100, 2),
            "sharpe": round(self.sharpe, 3),
            "max_drawdown_pct": round(self.max_drawdown * 100, 2),
            "win_rate_pct": round(self.win_rate * 100, 2),
            "num_trades": self.num_trades,
            "num_round_trips": self.num_round_trips,
        }


def compute(equity_curve: pd.Series, trades: list[Trade], initial_equity: float) -> PerformanceReport:
    if equity_curve.empty:
        return PerformanceReport(initial_equity, initial_equity, 0, 0, 0, 0, 0, 0, 0, 0)

    final_equity = float(equity_curve.iloc[-1])
    total_return = final_equity / initial_equity - 1.0

    # 연환산 지표
    daily = equity_curve.pct_change().dropna()
    ann_vol = float(daily.std() * np.sqrt(252)) if len(daily) > 1 else 0.0
    ann_ret = float(daily.mean() * 252) if len(daily) > 1 else 0.0
    sharpe = ann_ret / ann_vol if ann_vol > 0 else 0.0

    years = max((equity_curve.index[-1] - equity_curve.index[0]).days / 365.25, 1e-9)
    cagr = (final_equity / initial_equity) ** (1 / years) - 1 if initial_equity > 0 else 0.0

    # MDD
    running_max = equity_curve.cummax()
    drawdown = equity_curve / running_max - 1.0
    max_dd = float(drawdown.min()) if len(drawdown) else 0.0

    # 라운드트립 승률 계산 (FIFO)
    round_trips = _round_trips(trades)
    wins = sum(1 for pnl in round_trips if pnl > 0)
    win_rate = wins / len(round_trips) if round_trips else 0.0

    return PerformanceReport(
        initial_equity=initial_equity,
        final_equity=final_equity,
        total_return=total_return,
        cagr=cagr,
        annualized_vol=ann_vol,
        sharpe=sharpe,
        max_drawdown=max_dd,
        win_rate=win_rate,
        num_trades=len(trades),
        num_round_trips=len(round_trips),
    )


def _round_trips(trades: list[Trade]) -> list[float]:
    """FIFO 매칭으로 매수→매도 쌍의 순손익을 계산."""
    from collections import defaultdict, deque

    opens: dict[str, deque] = defaultdict(deque)
    pnls: list[float] = []
    for t in trades:
        if t.side is Side.BUY:
            opens[t.symbol].append([t.quantity, t.price, t.fee])
        else:
            remaining = t.quantity
            sell_cost = t.fee + t.tax
            while remaining > 0 and opens[t.symbol]:
                buy_qty, buy_price, buy_fee = opens[t.symbol][0]
                matched = min(buy_qty, remaining)
                pnl = (t.price - buy_price) * matched
                pnl -= (buy_fee * (matched / t.quantity if t.quantity else 0))
                pnl -= (sell_cost * (matched / t.quantity if t.quantity else 0))
                pnls.append(pnl)
                buy_qty -= matched
                remaining -= matched
                if buy_qty == 0:
                    opens[t.symbol].popleft()
                else:
                    opens[t.symbol][0][0] = buy_qty
    return pnls
