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

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

import structlog

from ._common import Bar, Side
from .account.balance import AccountSnapshot, get_balance
from .account.orders import place_order
from .config import Settings, get_settings
from .kiwoom.rest import KiwoomRestClient
from .kiwoom.ws import KiwoomWebSocket, Subscription
from .market.feed import Tick, ticks
from .notify.base import NoopNotifier, Notifier
from .notify.state import RunnerStateSnapshot
from .risk.manager import AccountState, Decision, RiskManager
from .strategy.base import Signal, Strategy

log = structlog.get_logger(__name__)


class RunnerControlState(StrEnum):
    """텔레그램 v3 (/pause /resume /stop) 으로 외부에서 토글 가능한 러너 제어 상태.

    - RUNNING : 정상 — 시그널·리스크·주문 전체 실행
    - PAUSED  : 시그널 기록·리스크 평가는 그대로, 실제 주문 전송만 건너뜀
                (텔레그램 /last_signal 정확도 유지하면서 일시 안전 모드)
    - STOPPING: 다음 tick 경계에서 graceful break, finally 블록으로 정리
    """

    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"


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
    return base - timedelta(minutes=base.minute % minutes)


@dataclass
class PaperRunner:
    """Paper/모의투자 러너.

    종목별로 다른 전략 인스턴스를 쓰려면 `strategies={"005930": s1, "000660": s2}` 형태로
    생성자에 직접 맵을 넘긴다. 생략하면 `strategy` 를 모든 종목에 공유.
    """

    strategy: Strategy | None = None
    symbols: list[str] = field(default_factory=list)
    strategies: dict[str, Strategy] = field(default_factory=dict)
    risk: RiskManager = field(default_factory=RiskManager)
    bar_minutes: int = 1
    balance_refresh_sec: int = 60
    allow_real: bool = False
    max_balance_failures: int = 5
    dry_run: bool = False  # True 면 모의투자 체결도 시뮬레이션(REST 전송 없음)
    notifier: Notifier = field(default_factory=NoopNotifier)
    _settings: Settings = field(default_factory=get_settings)
    _balance_failures: int = field(default=0, init=False)
    # 텔레그램 인커밍 명령 응답용 인메모리 상태 (runner 가 매 사이클 갱신)
    _snapshot: AccountSnapshot | None = field(default=None, init=False)
    _account: AccountState | None = field(default=None, init=False)
    _recent_signals: dict[str, Signal] = field(default_factory=dict, init=False)
    _started_at: datetime | None = field(default=None, init=False)
    # 텔레그램 v3 — 외부에서 pause/resume/request_stop 으로 토글
    _control_state: RunnerControlState = field(default=RunnerControlState.RUNNING, init=False)

    def __post_init__(self) -> None:
        # strategies 가 비어있으면 symbols 전부에 공통 strategy 를 복제 없이 공유.
        if not self.strategies:
            if self.strategy is None:
                raise ValueError("strategy 또는 strategies 중 하나는 반드시 지정해야 합니다")
            self.strategies = {sym: self.strategy for sym in self.symbols}
        elif not self.symbols:
            self.symbols = list(self.strategies.keys())
        missing = [sym for sym in self.symbols if sym not in self.strategies]
        if missing:
            raise ValueError(f"전략이 지정되지 않은 종목: {missing}")

    async def run(self) -> None:
        if not self._settings.is_mock and not self.allow_real:
            raise RuntimeError(
                "KIWOOM_MODE=real 에서 페이퍼 러너를 돌리려면 allow_real=True 를 명시하세요."
            )

        self._started_at = datetime.now()
        strat_names = sorted({type(s).__name__ for s in self.strategies.values()})
        await self.notifier.notify(
            "runner.started",
            strategy=",".join(strat_names),
            symbols=",".join(self.symbols),
            dry_run=self.dry_run,
        )

        stop_reason = "정상 종료"
        try:
            async with KiwoomRestClient(settings=self._settings) as rest:
                ws = KiwoomWebSocket(
                    settings=self._settings,
                    token_provider=rest.token_provider,
                )
                await ws.start([Subscription(symbols=self.symbols, types=["0B"])])

                aggregator = _BarAggregator(minutes=self.bar_minutes)
                account = await self._snapshot_account(rest, previous=None)
                last_refresh = datetime.now()

                for strat in set(self.strategies.values()):
                    strat.reset()
                try:
                    async for tick in ticks(ws):
                        # 외부 /stop 으로 STOPPING 진입 시 다음 tick 경계에서 graceful break.
                        if self._control_state is RunnerControlState.STOPPING:
                            log.info("runner.stop_requested", reason="control_state=stopping")
                            break
                        bar = aggregator.ingest(tick)
                        if bar is None:
                            continue
                        await self._on_bar(rest, bar, account)

                        if (
                            datetime.now() - last_refresh
                        ).total_seconds() >= self.balance_refresh_sec:
                            account = await self._snapshot_account(rest, previous=account)
                            last_refresh = datetime.now()
                finally:
                    await ws.stop()
        except KeyboardInterrupt:
            stop_reason = "KeyboardInterrupt"
            raise
        except Exception as exc:
            stop_reason = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            await self.notifier.notify("runner.stopped", reason=stop_reason)
            await self.notifier.aclose()

    async def _snapshot_account(
        self,
        rest: KiwoomRestClient,
        *,
        previous: AccountState | None,
    ) -> AccountState:
        try:
            snap: AccountSnapshot = await get_balance(rest)
        except Exception as exc:
            self._balance_failures += 1
            log.warning(
                "runner.balance_fetch_failed",
                error=str(exc),
                consecutive=self._balance_failures,
                max=self.max_balance_failures,
            )
            if previous is None:
                raise
            if self._balance_failures >= self.max_balance_failures:
                await self.notifier.notify(
                    "runner.balance_failed_fatal",
                    consecutive=self._balance_failures,
                )
                raise RuntimeError(
                    f"balance fetch failed {self._balance_failures} times in a row"
                ) from exc
            return previous

        self._balance_failures = 0
        positions = {h.symbol: h.quantity for h in snap.holdings}
        avg_prices = {h.symbol: float(h.avg_price) for h in snap.holdings}
        equity = float(snap.total_evaluation) or float(snap.deposit)
        day_start = previous.day_start_equity if previous and previous.day_start_equity else equity
        state = AccountState(
            deposit=float(snap.deposit),
            total_equity=equity,
            positions=positions,
            avg_prices=avg_prices,
            day_start_equity=day_start,
        )
        # 텔레그램 명령 응답을 위해 raw + derived 둘 다 보존.
        self._snapshot = snap
        self._account = state
        return state

    async def _on_bar(self, rest: KiwoomRestClient, bar: Bar, account: AccountState) -> None:
        log.debug("runner.bar", symbol=bar.symbol, ts=bar.ts.isoformat(), close=bar.close)
        strat = self.strategies.get(bar.symbol)
        if strat is None:
            return
        for signal in strat.on_bar(bar):
            # 시그널은 리스크 거절·체결 여부와 무관하게 이력에 기록 (텔레그램 /last_signal 용).
            self._recent_signals[signal.symbol] = signal
            decision = self.risk.evaluate(signal, price=bar.close, account=account)
            if decision.decision is Decision.REJECT or decision.order is None:
                log.info(
                    "runner.signal_rejected",
                    symbol=signal.symbol,
                    side=signal.side.value,
                    reason=decision.reason,
                )
                continue
            order = decision.order
            # /pause 진입 시 — 시그널·리스크는 그대로 통과시키고 실제 주문 송신만 건너뜀.
            # 이래야 텔레그램 /last_signal 이 "지금 무엇을 보고 있는지" 정확히 보여줌.
            if self._control_state is RunnerControlState.PAUSED:
                log.info(
                    "runner.order_skipped_paused",
                    symbol=order.symbol,
                    side=order.side.value,
                    qty=order.quantity,
                )
                continue
            if self.dry_run:
                log.info(
                    "runner.order_simulated",
                    symbol=order.symbol,
                    side=order.side.value,
                    qty=order.quantity,
                    price=float(order.price) if order.price else bar.close,
                )
                continue
            try:
                result = await place_order(rest, order)
                log.info(
                    "runner.order_accepted",
                    order_no=result.order_no,
                    symbol=order.symbol,
                    side=order.side.value,
                    qty=order.quantity,
                )
                await self.notifier.notify(
                    "runner.order_accepted",
                    order_no=result.order_no,
                    symbol=order.symbol,
                    side=order.side.value,
                    qty=order.quantity,
                )
                # 로컬 포지션 즉시 갱신 (다음 snapshot 까지의 공백 메움)
                delta = order.quantity if order.side is Side.BUY else -order.quantity
                account.positions[order.symbol] = max(
                    0, account.positions.get(order.symbol, 0) + delta
                )
                if order.side is Side.BUY:
                    account.avg_prices[order.symbol] = float(order.price or Decimal(str(bar.close)))
            except Exception as exc:
                log.error(
                    "runner.order_failed",
                    error=str(exc),
                    symbol=order.symbol,
                    exc_info=exc,
                )
                await self.notifier.notify(
                    "runner.order_failed",
                    error=str(exc),
                    symbol=order.symbol,
                    side=order.side.value,
                    qty=order.quantity,
                )

    def state_snapshot(self) -> RunnerStateSnapshot:
        """텔레그램 명령 핸들러가 호출하는 읽기 전용 스냅샷.

        얕은 복사로 내부 상태 race 를 막는다 (핸들러는 별도 task 에서 동작).
        """
        return RunnerStateSnapshot(
            mode=self._settings.kiwoom_mode.value,
            dry_run=self.dry_run,
            symbols=list(self.symbols),
            strategies={sym: type(strat).__name__ for sym, strat in self.strategies.items()},
            started_at=self._started_at,
            snapshot=self._snapshot,
            account=self._account,
            recent_signals=dict(self._recent_signals),
            balance_failures=self._balance_failures,
            control_state=self._control_state.value,
        )

    # ---- 텔레그램 v3 외부 제어 (모두 동기. async loop 가 폴링) -------------

    def pause(self) -> None:
        """주문 송신을 일시 보류. 시그널 기록·리스크 평가는 그대로 진행.

        STOPPING 상태에서는 무시 — 종료 진행 중에 일시정지 의미 없음.
        """
        if self._control_state is RunnerControlState.STOPPING:
            return
        self._control_state = RunnerControlState.PAUSED
        log.info("runner.control_state_changed", state="paused")

    def resume(self) -> None:
        """일시 보류 해제. STOPPING 에서는 복귀 불가."""
        if self._control_state is RunnerControlState.STOPPING:
            return
        self._control_state = RunnerControlState.RUNNING
        log.info("runner.control_state_changed", state="running")

    def request_stop(self) -> None:
        """다음 tick 경계에서 graceful 종료 요청. 한 번 진입하면 되돌릴 수 없음."""
        self._control_state = RunnerControlState.STOPPING
        log.info("runner.control_state_changed", state="stopping")
