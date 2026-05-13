"""Shadow Strategy Tracker — 29단계 PR42 (#7).

무엇/왜:
- 라이브 데이터(실시간 가격) 에서 *주문 송신 없이* 신규 전략의 가상 PnL 누적.
- 백테스트 → paper-day → 실거래 흐름 중 *paper-day 와 실거래 사이* 의 마지막 검증층.
- 결과 jsonl 로 영속 → 1주일 누적 후 백테스트 결과와 PnL 차이 비교 → "백테스트와
  라이브 괴리" 정량 진단.

설계 결정:
- *결정론 보존*: 가격·시그널은 호출자가 주입. 모듈은 누적·기록만.
- *주문 X*: 실제 주문 송신 절대 안 함. 가상 PnL 만 계산.
- *모든 BUY 즉시 체결 + SELL 매칭 FIFO*: 단순 라이브 시뮬. 슬리피지·수수료는 옵션.
- *jsonl 영속*: 봇 재시작 후에도 누적 PnL 복구 가능.

어떻게 확인: tests/test_shadow_tracker.py.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .._common import Side

__all__ = ["ShadowSignal", "ShadowSnapshot", "ShadowTracker"]


@dataclass(frozen=True)
class ShadowSignal:
    """단일 가상 거래 이벤트 (호출자가 주입)."""

    symbol: str
    side: Side
    quantity: int
    price: float
    ts: datetime


@dataclass(frozen=True)
class ShadowSnapshot:
    """현 시점 누적 상태 — 보유 / 실현 PnL / 평가."""

    cash: float  # 가상 현금 (초기 → 매수 차감, 매도 증가)
    realized_pnl: float  # FIFO 매칭 실현 PnL 누적
    holdings: dict[str, int]  # symbol → 수량
    avg_prices: dict[str, float]  # symbol → 평균 매수가
    num_signals: int  # 처리된 시그널 총 개수


@dataclass
class ShadowTracker:
    """가상 포지션·PnL 누적기. 외부 호출 0 — 시그널·현재가만 받음.

    매개변수:
    - ``initial_cash``: 가상 시작 자본.
    - ``commission_rate`` / ``tax_rate_sell`` / ``slippage_bps``: 백테스트와 동일한
      가정으로 일관성 ↑. 기본은 보수적 (SimulatedBroker 와 같은 값).
    - ``log_path``: 시그널 jsonl 누적 경로. None 이면 메모리만.
    """

    initial_cash: float = 10_000_000.0
    commission_rate: float = 0.00015
    tax_rate_sell: float = 0.0020
    slippage_bps: float = 5.0
    log_path: Path | None = None
    cash: float = field(init=False)
    realized_pnl: float = field(default=0.0, init=False)
    holdings: dict[str, int] = field(default_factory=dict, init=False)
    avg_prices: dict[str, float] = field(default_factory=dict, init=False)
    _fifo_lots: dict[str, deque] = field(default_factory=dict, init=False)
    num_signals: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.cash = self.initial_cash

    def apply(self, signal: ShadowSignal) -> None:
        """가상 시그널 1개 적용 — 주문 X, 메모리·디스크만 갱신."""
        if signal.quantity <= 0:
            return
        slip = signal.price * (self.slippage_bps / 10_000.0)
        if signal.side is Side.BUY:
            price = signal.price + slip
            gross = price * signal.quantity
            fee = gross * self.commission_rate
            cost = gross + fee
            self.cash -= cost
            held = self.holdings.get(signal.symbol, 0)
            avg = self.avg_prices.get(signal.symbol, 0.0)
            total = avg * held + price * signal.quantity
            new_qty = held + signal.quantity
            self.holdings[signal.symbol] = new_qty
            self.avg_prices[signal.symbol] = total / new_qty if new_qty else 0.0
            self._fifo_lots.setdefault(signal.symbol, deque()).append([signal.quantity, price])
        else:
            price = signal.price - slip
            held = self.holdings.get(signal.symbol, 0)
            qty = min(signal.quantity, held)
            if qty <= 0:
                return  # 보유 없는데 SELL → 무시 (라이브 환경 안전).
            gross = price * qty
            fee = gross * self.commission_rate
            tax = gross * self.tax_rate_sell
            self.cash += gross - fee - tax
            # FIFO 매칭 PnL.
            remaining = qty
            lots = self._fifo_lots.setdefault(signal.symbol, deque())
            while remaining > 0 and lots:
                lot_qty, lot_price = lots[0]
                matched = min(lot_qty, remaining)
                pnl = (price - lot_price) * matched - (fee + tax) * (matched / qty)
                self.realized_pnl += pnl
                lot_qty -= matched
                remaining -= matched
                if lot_qty == 0:
                    lots.popleft()
                else:
                    lots[0][0] = lot_qty
            self.holdings[signal.symbol] = held - qty
            if self.holdings[signal.symbol] == 0:
                self.avg_prices.pop(signal.symbol, None)
        self.num_signals += 1
        if self.log_path is not None:
            self._append_log(signal)

    def snapshot(self) -> ShadowSnapshot:
        return ShadowSnapshot(
            cash=self.cash,
            realized_pnl=self.realized_pnl,
            holdings=dict(self.holdings),
            avg_prices=dict(self.avg_prices),
            num_signals=self.num_signals,
        )

    def equity(self, marks: dict[str, float]) -> float:
        """현재가 dict 로 보유 평가 + 현금. 평가가 없으면 평단가 fallback."""
        held_value = sum(
            qty * marks.get(sym, self.avg_prices.get(sym, 0.0))
            for sym, qty in self.holdings.items()
        )
        return self.cash + held_value

    def _append_log(self, signal: ShadowSignal) -> None:
        if self.log_path is None:
            return
        record = {
            "ts": signal.ts.isoformat(),
            "symbol": signal.symbol,
            "side": signal.side.value,
            "quantity": signal.quantity,
            "price": signal.price,
            "cash_after": self.cash,
            "realized_pnl_after": self.realized_pnl,
        }
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(record, ensure_ascii=False) + "\n")

    @classmethod
    def replay(cls, log_path: Path) -> Iterator[dict]:
        """기록된 jsonl 을 한 줄씩 yield — 누적 PnL 외부에서 재구성용."""
        if not log_path.exists():
            return
        with log_path.open("r", encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                yield json.loads(line)
