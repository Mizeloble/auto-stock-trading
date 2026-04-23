"""라이브(모의) 매매 루프.

흐름:
1. 토큰 발급 → REST/WS 클라이언트 준비
2. 종목 구독, 실시간 Tick 수신
3. Tick 을 1분봉으로 집계
4. 봉 확정 시점에 전략 `on_bar()` 호출 → Signal
5. 리스크 매니저에 통과시켜 Order 생성
6. 주문 전송 + 로그
7. 주기적으로 잔고 갱신

주의: 실거래(`KIWOOM_MODE=real`) 에서 이 러너를 돌리려면 `allow_real=True` 를 명시 지정해야 한다.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

import structlog

from .account.balance import AccountSnapshot, get_balance
from .account.orders import place_order
from .config import Settings, get_settings
from .kiwoom.rest import KiwoomRestClient
from .kiwoom.ws import KiwoomWebSocket, Subscription
from .market.feed import Tick, ticks
from .risk.manager import AccountState, Decision, RiskManager
from .strategy.base import Bar, Strategy

log = structlog.get_logger(__name__)


@dataclass
class _BarAggregator:
    """Tick 들을 분(또는 설정된 길이)봉으로 집계."""

    minutes: int = 1
    _current: dict[str, Bar] = field(default_factory=dict)
    _bucket_end: dict[str, datetime] = field(default_factory=dict)

    def ingest(self, tick: Tick) -> Bar | None:
        """Bar 가 확정되면 그 Bar 를 반환. 진행 중이면 None."""
        bucket_end = _floor_bucket(tick.ts, self.minutes) + timedelta(minutes=self.minutes)
        prev_bar = self._current.get(tick.symbol)
        prev_end = self._bucket_end.get(tick.symbol)

        finalized: Bar | None = None
        if prev_bar is not None and prev_end is not None and tick.ts >= prev_end:
            finalized = prev_bar
            prev_bar = None

        price = float(tick.price)
        volume = max(0, tick.volume)
        if prev_bar is None:
            self._current[tick.symbol] = Bar(
                symbol=tick.symbol,
                ts=bucket_end - timedelta(minutes=self.minutes),
                open=price,
                high=price,
                low=price,
                close=price,
                volume=volume,
            )
            self._bucket_end[tick.symbol] = bucket_end
        else:
            self._current[tick.symbol] = Bar(
                symbol=prev_bar.symbol,
                ts=prev_bar.ts,
                open=prev_bar.open,
                high=max(prev_bar.high, price),
                low=min(prev_bar.low, price),
                close=price,
                volume=prev_bar.volume + volume,
            )
        return finalized


def _floor_bucket(ts: datetime, minutes: int) -> datetime:
    base = ts.replace(second=0, microsecond=0)
    floor = base - timedelta(minutes=base.minute % minutes)
    return floor


@dataclass
class PaperRunner:
    strategy: Strategy
    symbols: list[str]
    risk: RiskManager = field(default_factory=RiskManager)
    bar_minutes: int = 1
    balance_refresh_sec: int = 60
    allow_real: bool = False
    _settings: Settings = field(default_factory=get_settings)

    async def run(self) -> None:
        if not self._settings.is_mock and not self.allow_real:
            raise RuntimeError(
                "KIWOOM_MODE=real 에서 페이퍼 러너를 돌리려면 allow_real=True 를 명시하세요."
            )

        async with KiwoomRestClient(settings=self._settings) as rest:
            ws = KiwoomWebSocket(
                settings=self._settings,
                token_provider=rest._token_provider,  # 토큰 공유
            )
            await ws.start([Subscription(symbols=self.symbols, types=["0B"])])

            aggregator = _BarAggregator(minutes=self.bar_minutes)
            account = await self._snapshot_account(rest)
            last_refresh = datetime.now()

            self.strategy.reset()
            try:
                async for tick in ticks(ws):
                    bar = aggregator.ingest(tick)
                    if bar is None:
                        continue
                    await self._on_bar(rest, bar, account)

                    if (datetime.now() - last_refresh).total_seconds() >= self.balance_refresh_sec:
                        account = await self._snapshot_account(rest)
                        last_refresh = datetime.now()
            finally:
                await ws.stop()

    async def _snapshot_account(self, rest: KiwoomRestClient) -> AccountState:
        try:
            snap: AccountSnapshot = await get_balance(rest)
        except Exception as exc:
            log.warning("runner.balance_fetch_failed", error=str(exc))
            return AccountState(deposit=0, total_equity=0)

        positions = {h.symbol: h.quantity for h in snap.holdings}
        avg_prices = {h.symbol: float(h.avg_price) for h in snap.holdings}
        equity = float(snap.total_evaluation) or float(snap.deposit)
        state = AccountState(
            deposit=float(snap.deposit),
            total_equity=equity,
            positions=positions,
            avg_prices=avg_prices,
        )
        if state.day_start_equity == 0:
            state.day_start_equity = equity
        return state

    async def _on_bar(self, rest: KiwoomRestClient, bar: Bar, account: AccountState) -> None:
        log.debug("runner.bar", symbol=bar.symbol, ts=bar.ts.isoformat(), close=bar.close)
        signals = self.strategy.on_bar(bar)
        for signal in signals:
            decision = self.risk.evaluate(signal, price=bar.close, account=account)
            if decision.decision is Decision.REJECT or decision.order is None:
                log.info(
                    "runner.signal_rejected",
                    symbol=signal.symbol,
                    side=signal.side.value,
                    reason=decision.reason,
                )
                continue
            try:
                result = await place_order(rest, decision.order)
                log.info(
                    "runner.order_accepted",
                    order_no=result.order_no,
                    symbol=decision.order.symbol,
                    side=decision.order.side.value,
                    qty=decision.order.quantity,
                )
                # 로컬 포지션 즉시 갱신 (다음 snapshot 까지의 공백 메움)
                qty_delta = decision.order.quantity if decision.order.side.value == "buy" else -decision.order.quantity
                account.positions[decision.order.symbol] = max(
                    0, account.positions.get(decision.order.symbol, 0) + qty_delta
                )
                if decision.order.side.value == "buy":
                    account.avg_prices[decision.order.symbol] = float(decision.order.price or Decimal(str(bar.close)))
            except Exception as exc:
                log.error("runner.order_failed", error=str(exc), symbol=decision.order.symbol)
