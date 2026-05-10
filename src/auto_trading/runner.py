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
import contextlib
import signal as _signal
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum

import structlog

from ._common import KST, Bar, Side
from .account.balance import AccountSnapshot, get_balance
from .account.orders import Order, OrderType, place_order
from .config import Settings, get_settings
from .kiwoom.rest import KiwoomRestClient
from .kiwoom.ws import KiwoomWebSocket, Subscription
from .market.feed import Tick, ticks
from .notify.base import NoopNotifier, Notifier
from .notify.state import RunnerStateSnapshot
from .risk.manager import AccountState, Decision, RiskManager
from .risk.position_guard import GuardAction, PositionGuard
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
    # 23단계 Phase C — 종목별 stop/trailing/partial. 기본 비활성(모두 0). risk.limits 참조.
    position_guard: PositionGuard | None = None
    bar_minutes: int = 1
    balance_refresh_sec: int = 60
    allow_real: bool = False
    max_balance_failures: int = 5
    dry_run: bool = False  # True 면 모의투자 체결도 시뮬레이션(REST 전송 없음)
    notifier: Notifier = field(default_factory=NoopNotifier)
    # day-trade 모드: KST 청산 시각. None 이면 swing(기존) 동작.
    # 설정되면 (a) 시작 시 빈 universe 허용 + set_universe() 대기, (b) 백그라운드
    # 시계 task 가 도달 시 liquidate_all → request_stop 으로 자기 종료.
    liquidate_at: time | None = None
    liquidate_grace_sec: int = 30
    _settings: Settings = field(default_factory=get_settings)
    _balance_failures: int = field(default=0, init=False)
    # 텔레그램 인커밍 명령 응답용 인메모리 상태 (runner 가 매 사이클 갱신)
    _snapshot: AccountSnapshot | None = field(default=None, init=False)
    _account: AccountState | None = field(default=None, init=False)
    _recent_signals: dict[str, Signal] = field(default_factory=dict, init=False)
    _started_at: datetime | None = field(default=None, init=False)
    # 텔레그램 v3 — 외부에서 pause/resume/request_stop 으로 토글
    _control_state: RunnerControlState = field(default=RunnerControlState.RUNNING, init=False)
    # request_stop 시 ws.messages() 루프에 즉시 신호를 주기 위한 활성 ws 참조.
    _ws: KiwoomWebSocket | None = field(default=None, init=False, repr=False)
    # day-trade 모드: set_universe() 가 채워질 때까지 run() 가 대기하는 이벤트.
    _universe_ready: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    # 청산 1회 가드 — 장 마감 후 시계가 계속 도달 상태라도 두 번 호출되지 않게.
    _liquidation_done: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        # 23단계 Phase C — 명시 주입 안 했으면 risk.limits 를 그대로 따르는 PositionGuard 자동 생성.
        if self.position_guard is None:
            self.position_guard = PositionGuard(limits=self.risk.limits)

        # day-trade 모드(liquidate_at 설정)는 시작 시 universe 미정 허용.
        # set_universe() 호출 전까지 run() 이 대기.
        if not self.symbols and not self.strategies:
            if self.liquidate_at is None:
                raise ValueError("strategy 또는 strategies 중 하나는 반드시 지정해야 합니다")
            return  # day-trade 모드 — 검증은 set_universe() 안에서.

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
        self._universe_ready.set()  # 시작 전부터 universe 가 있으면 즉시 ready.

    def _install_signal_handlers(self) -> None:
        """SIGTERM/SIGINT 을 graceful stop (`request_stop`) 으로 라우팅.

        무엇/왜:
        - launchd SIGTERM (day-trade 자체 청산 후 종료, 또는 legacy `paper-stop.sh`)
          이나 사용자 Ctrl+C 가 와도 텔레그램 `/stop` 과 같은 경로로 들어가 finally
          블록 (`runner.stopped` 푸시 + notifier aclose) 이 정상 동작. 핸들러 없으면
          Python 이 즉시 죽어 종료 푸시·로그 flush 실패.
        - Windows 또는 별도 스레드 환경에서 `add_signal_handler` 가 NotImplementedError /
          RuntimeError 를 내면 조용히 무시 — 운영 환경(macOS launchd) 은 영향 없음.

        어떻게 확인: 종료 신호 후 logs/paper-*.log 끝줄에 `runner.stop_requested`
        + `runner.stopped` 이 모두 찍히고 텔레그램에 "⏹ paper 종료" 푸시 도착.
        """
        loop = asyncio.get_running_loop()
        for sig in (_signal.SIGTERM, _signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, self.request_stop)

    async def run(self) -> None:
        if not self._settings.is_mock and not self.allow_real:
            raise RuntimeError(
                "KIWOOM_MODE=real 에서 페이퍼 러너를 돌리려면 allow_real=True 를 명시하세요."
            )

        self._install_signal_handlers()
        self._started_at = datetime.now()

        # day-trade 모드: orchestrator 가 발굴 후 set_universe() 를 부를 때까지 대기.
        # 그동안 STOPPING 진입(텔레그램 /stop, SIGTERM)은 즉시 빠져나가야 함.
        if not self._universe_ready.is_set():
            log.info("runner.awaiting_universe", liquidate_at=str(self.liquidate_at))
            done = await self._wait_universe_or_stop()
            if not done:
                # universe 가 채워지기 전에 외부 stop. notifier 만 깔끔히 정리하고 종료.
                await self.notifier.notify("runner.stopped", reason="stopped_before_universe")
                await self.notifier.aclose()
                return

        strat_names = sorted({type(s).__name__ for s in self.strategies.values()})
        await self.notifier.notify(
            "runner.started",
            strategy=",".join(strat_names),
            symbols=",".join(self.symbols),
            dry_run=self.dry_run,
        )

        stop_reason = "정상 종료"
        liquidation_task: asyncio.Task[None] | None = None
        try:
            async with KiwoomRestClient(settings=self._settings) as rest:
                ws = KiwoomWebSocket(
                    settings=self._settings,
                    token_provider=rest.token_provider,
                )
                # request_stop 이 ws.signal_stop 을 부를 수 있게 활성 ws 참조 보관.
                self._ws = ws
                await ws.start([Subscription(symbols=self.symbols, types=["0B"])])

                aggregator = _BarAggregator(minutes=self.bar_minutes)
                account = await self._snapshot_account(rest, previous=None)
                last_refresh = datetime.now()

                for strat in set(self.strategies.values()):
                    strat.reset()

                # day-trade 모드면 청산 시계 task 백그라운드로. tick 이 안 와도 청산이 도는 게 핵심.
                if self.liquidate_at is not None:
                    liquidation_task = asyncio.create_task(self._liquidation_loop(rest))

                try:
                    async for tick in ticks(ws):
                        # === STOPPING 체크 ===
                        # 외부 /stop 으로 STOPPING 진입 시 다음 tick 경계에서 graceful break.
                        if self._control_state is RunnerControlState.STOPPING:
                            log.info("runner.stop_requested", reason="control_state=stopping")
                            break

                        # === tick → bar aggregation ===
                        bar = aggregator.ingest(tick)
                        if bar is None:
                            continue

                        # === 봉 확정 시점 전략 평가 + 주문 ===
                        await self._on_bar(rest, bar, account)

                        # === 주기적 잔고 갱신 + kill_switch ===
                        if (
                            datetime.now() - last_refresh
                        ).total_seconds() >= self.balance_refresh_sec:
                            account = await self._snapshot_account(rest, previous=account)
                            last_refresh = datetime.now()
                            await self._check_kill_switch(rest, account)
                finally:
                    self._ws = None
                    await ws.stop()
        except KeyboardInterrupt:
            stop_reason = "KeyboardInterrupt"
            raise
        except Exception as exc:
            stop_reason = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if liquidation_task is not None:
                liquidation_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await liquidation_task
            await self.notifier.notify("runner.stopped", reason=stop_reason)
            await self.notifier.aclose()

    async def _check_kill_switch(self, rest: KiwoomRestClient, account: AccountState) -> None:
        """일일 손실 한도 도달 + `daily_loss_kill` 활성 시 강제 청산 + request_stop.

        23단계 Phase B 의 kill switch. balance refresh 직후 메인 루프가 호출.
        `liquidate_all` 은 `_liquidation_done` 가드로 idempotent — 두 번 호출돼도 안전.
        """
        if not self.risk.should_force_liquidate(account):
            return
        log.warning(
            "runner.kill_switch_triggered",
            today_change=(
                account.total_equity / account.day_start_equity - 1.0
                if account.day_start_equity
                else 0.0
            ),
            daily_loss_limit=self.risk.limits.daily_loss_limit,
        )
        await self.notifier.notify(
            "runner.kill_switch_triggered",
            daily_loss_limit=self.risk.limits.daily_loss_limit,
        )
        await self.liquidate_all(rest, account, reason="daily_loss_kill")
        self.request_stop()

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
        # 23단계 Phase B — 잔고 갱신은 누적 매수대금을 리셋하지 않음 (러너 재시작 = 자정 리셋).
        today_notional = previous.today_notional_used if previous is not None else 0.0
        state = AccountState(
            deposit=float(snap.deposit),
            total_equity=equity,
            positions=positions,
            avg_prices=avg_prices,
            day_start_equity=day_start,
            today_notional_used=today_notional,
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
            order = await self._evaluate_signal(signal, bar, account)
            if order is None:
                continue
            await self._place_and_track(rest, order, bar, account)

        # 23단계 Phase C — 매 봉 끝에 PositionGuard 체크. 보유 0 이면 자동 no-op.
        await self._run_position_guard(rest, bar, account)

    async def _run_position_guard(
        self, rest: KiwoomRestClient, bar: Bar, account: AccountState
    ) -> None:
        """guard 활성 + 해당 종목 보유 시 stop/trailing/partial 결정 → 즉시 SELL 송신."""
        guard = self.position_guard
        if guard is None or not guard.is_active:
            return
        qty = account.positions.get(bar.symbol, 0)
        if qty <= 0:
            return
        avg = account.avg_prices.get(bar.symbol, 0.0)
        if avg <= 0:
            return
        action = guard.check(symbol=bar.symbol, price=bar.close, quantity=qty, avg_price=avg)
        if action is None:
            return
        await self._place_guard_action(rest, action, bar, account)

    async def _place_guard_action(
        self,
        rest: KiwoomRestClient,
        action: GuardAction,
        bar: Bar,
        account: AccountState,
    ) -> None:
        """guard 발화 → SELL 주문(시장가). dry-run·PAUSED 도 동일 분기."""
        order = Order(
            symbol=action.symbol,
            side=Side.SELL,
            quantity=action.quantity,
            order_type=OrderType.MARKET,
            price=None,
        )
        if self._control_state is RunnerControlState.PAUSED:
            log.info(
                "position_guard.skipped_paused",
                symbol=action.symbol,
                reason=action.reason.value,
                qty=action.quantity,
            )
            return
        if self.dry_run:
            log.info(
                "runner.order_simulated",
                symbol=action.symbol,
                side="sell",
                qty=action.quantity,
                price=action.price,
                guard_reason=action.reason.value,
            )
            # dry-run 도 로컬 포지션은 즉시 차감 — 다음 guard.check 가 잔량을 정확히 읽도록.
            account.positions[action.symbol] = max(
                0, account.positions.get(action.symbol, 0) - action.quantity
            )
            await self.notifier.notify(
                "position_guard.fired",
                symbol=action.symbol,
                reason=action.reason.value,
                qty=action.quantity,
                price=action.price,
                dry_run=True,
            )
            # 23단계 Phase D — quantity 0 면 last_exit_ts + guard reset.
            if account.positions.get(action.symbol, 0) <= 0:
                self._mark_exit(action.symbol, bar.ts)
            return
        # 실거래 송신 — _place_and_track 재사용. 성공 시 로컬 포지션 차감 + 알림은 그쪽이 처리.
        await self._place_and_track(rest, order, bar, account)
        await self.notifier.notify(
            "position_guard.fired",
            symbol=action.symbol,
            reason=action.reason.value,
            qty=action.quantity,
            price=action.price,
            dry_run=False,
        )

    async def _evaluate_signal(
        self, signal: Signal, bar: Bar, account: AccountState
    ) -> Order | None:
        """리스크 평가 + PAUSED/dry-run 게이트.

        None 반환 케이스(각각 로그 한 줄):
        - 리스크 매니저 REJECT → `runner.signal_rejected`
        - PAUSED → `runner.order_skipped_paused` (텔레그램 /last_signal 정확도 위해 시그널·리스크는 그대로)
        - dry_run → `runner.order_simulated`
        Order 반환 시 호출자가 _place_and_track 으로 실제 송신.
        """
        decision = self.risk.evaluate(signal, price=bar.close, account=account)
        if decision.decision is Decision.REJECT or decision.order is None:
            log.info(
                "runner.signal_rejected",
                symbol=signal.symbol,
                side=signal.side.value,
                reason=decision.reason,
            )
            return None
        order = decision.order
        if self._control_state is RunnerControlState.PAUSED:
            log.info(
                "runner.order_skipped_paused",
                symbol=order.symbol,
                side=order.side.value,
                qty=order.quantity,
            )
            return None
        if self.dry_run:
            sim_price = float(order.price) if order.price else bar.close
            log.info(
                "runner.order_simulated",
                symbol=order.symbol,
                side=order.side.value,
                qty=order.quantity,
                price=sim_price,
            )
            # 23단계 Phase B — dry-run BUY 도 일일 누적에 포함 (한도 검증 회로 똑같이 흘러야).
            if order.side is Side.BUY:
                account.today_notional_used += sim_price * order.quantity
            return None
        return order

    async def _place_and_track(
        self, rest: KiwoomRestClient, order: Order, bar: Bar, account: AccountState
    ) -> None:
        """REST 송신 + 성공·실패 분기. 성공 시 로컬 포지션·평단 즉시 갱신, notifier 양방향."""
        try:
            result = await place_order(rest, order)
            # 시장가는 order.price=None. 봉 종가로 근사 — 정확한 체결가는 사후 잔고 갱신 시점에.
            ref_price = float(order.price or Decimal(str(bar.close)))
            log.info(
                "runner.order_accepted",
                order_no=result.order_no,
                symbol=order.symbol,
                side=order.side.value,
                qty=order.quantity,
                price=ref_price,
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
            account.positions[order.symbol] = max(0, account.positions.get(order.symbol, 0) + delta)
            if order.side is Side.BUY:
                account.avg_prices[order.symbol] = ref_price
                # 23단계 Phase B — 일일 매수대금 누적. 실패 시(except 분기) 누적 X.
                account.today_notional_used += ref_price * order.quantity
            # 23단계 Phase D — SELL 로 잔량 0 이면 last_exit_ts 갱신 + guard reset.
            if order.side is Side.SELL and account.positions.get(order.symbol, 0) <= 0:
                self._mark_exit(order.symbol, bar.ts)
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

    def _mark_exit(self, symbol: str, ts: datetime) -> None:
        """SELL 로 잔량 0 이 됐을 때의 정리 — 23단계 Phase D.

        - 해당 종목의 strategy.state["last_exit_ts"] = ts (다회 진입 전략의 쿨다운 기준).
        - PositionGuard.reset(symbol) — 재진입 시 highest_seen·partial_taken 초기화.
        """
        strat = self.strategies.get(symbol)
        if strat is not None:
            strat.state["last_exit_ts"] = ts
        if self.position_guard is not None:
            self.position_guard.reset(symbol)

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
            runner_mode="day-trade" if self.liquidate_at is not None else "swing",
            liquidate_at_label=(
                self.liquidate_at.strftime("%H:%M") if self.liquidate_at is not None else ""
            ),
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
        """graceful 종료 요청. 한 번 진입하면 되돌릴 수 없음.

        무엇/왜:
        - control_state 만 바꾸면 ticks(ws) 가 다음 메시지를 영원히 기다릴 때
          (예: 장 마감 후 키움이 0B 시세를 안 보낼 때) STOPPING 체크 코드까지
          못 도달해 main loop hang. 4/30 launchd 자동 정지에서 7시간 hang 실증.
        - 그래서 ws.signal_stop() 을 같이 호출 — ws.messages() 의 1초 폴링
          루프가 다음 사이클에서 _stop 을 보고 즉시 종료 → ticks() 종료 →
          main loop 의 `async for` 정상 break → finally → notifier "runner.stopped".
        - day-trade 모드에서 universe 대기 중에도 호출 가능 — _universe_ready
          를 set 해서 _wait_universe_or_stop() 이 깨어나 종료 분기를 타도록.
        """
        self._control_state = RunnerControlState.STOPPING
        log.info("runner.control_state_changed", state="stopping")
        ws = self._ws
        if ws is not None:
            ws.signal_stop()
        # day-trade: universe 대기 중일 수 있으니 같이 깨움.
        self._universe_ready.set()

    # ---- day-trade 모드 (21단계 Phase B) -----------------------------------

    def set_universe(
        self,
        symbols: list[str],
        strategies: dict[str, Strategy],
    ) -> None:
        """orchestrator 가 발굴 결과를 주입할 때 호출.

        무엇/왜:
        - day-trade 모드는 시작 시점에 매수 대상이 미정 — 09:00:30 발굴 후 주입.
        - 호출되면 run() 내부의 `_wait_universe_or_stop()` 가 깨어나 ws.start 진행.
        - 시작 후(ws 활성) 변경은 PR-B 범위 밖 — 시작 전 1회만 허용.
        """
        if self._ws is not None:
            raise RuntimeError("set_universe() 는 run() 진입 전, ws.start() 이전에만 호출하세요.")
        if not symbols:
            raise ValueError("symbols 가 비어있습니다")
        missing = [s for s in symbols if s not in strategies]
        if missing:
            raise ValueError(f"전략이 지정되지 않은 종목: {missing}")
        self.symbols = list(symbols)
        self.strategies = dict(strategies)
        self._universe_ready.set()
        log.info(
            "runner.universe_set",
            symbols=list(self.symbols),
            strategies={s: type(self.strategies[s]).__name__ for s in self.symbols},
        )

    async def _wait_universe_or_stop(self) -> bool:
        """universe 가 채워지거나 stop 요청이 오기까지 대기.

        반환 True = universe 채워져 정상 진행, False = stop 으로 종료해야 함.
        """
        await self._universe_ready.wait()
        return self._control_state is not RunnerControlState.STOPPING and bool(self.symbols)

    async def liquidate_all(
        self,
        rest: KiwoomRestClient,
        account: AccountState,
        *,
        reason: str,
    ) -> None:
        """보유 모든 종목을 시장가 SELL 송신. dry_run 이면 시뮬 로그만.

        한 종목 실패는 다른 종목으로 진행. 호출 후 _liquidation_done 마킹 — 두 번 호출 X.
        """
        if self._liquidation_done:
            log.info("runner.liquidate_skipped", reason="already_done")
            return
        positions = {sym: qty for sym, qty in account.positions.items() if qty > 0}
        if not positions:
            log.info("runner.liquidate_noop", reason="no_positions")
            self._liquidation_done = True
            return

        log.info(
            "runner.liquidate_start",
            reason=reason,
            symbols=list(positions),
            dry_run=self.dry_run,
        )
        # 2026-05-08 회귀 — 일부 종목이 실패해도 "청산 완료 · N종목" 으로 푸시되어
        # 사용자가 전부 청산된 줄 오해. 성공/실패를 분리 카운트해 알림에 둘 다 노출.
        success = 0
        failed = 0
        for symbol, qty in positions.items():
            order = Order(
                symbol=symbol,
                side=Side.SELL,
                quantity=qty,
                order_type=OrderType.MARKET,
            )
            buy_price = account.avg_prices.get(symbol)
            if self.dry_run:
                log.info(
                    "runner.liquidate_simulated",
                    symbol=symbol,
                    side="sell",
                    qty=qty,
                    reason=reason,
                    buy_price=buy_price,
                )
                # 시뮬에서도 로컬 포지션 즉시 0 으로 — 다음 청산 호출이 idempotent.
                account.positions[symbol] = 0
                success += 1
                continue
            try:
                result = await place_order(rest, order)
                log.info(
                    "runner.liquidate_accepted",
                    order_no=result.order_no,
                    symbol=symbol,
                    qty=qty,
                    buy_price=buy_price,
                )
                account.positions[symbol] = 0
                success += 1
            except Exception as exc:
                failed += 1
                log.error(
                    "runner.liquidate_failed",
                    symbol=symbol,
                    qty=qty,
                    error=str(exc),
                    exc_info=exc,
                )
                await self.notifier.notify(
                    "runner.liquidate_failed",
                    symbol=symbol,
                    qty=qty,
                    error=str(exc),
                )

        self._liquidation_done = True
        # count 는 성공 건수만 (backward-compatible). failed 가 추가 필드.
        await self.notifier.notify(
            "runner.liquidate_done",
            reason=reason,
            count=success,
            failed=failed,
            attempted=len(positions),
            dry_run=self.dry_run,
        )

    async def _liquidation_loop(self, rest: KiwoomRestClient) -> None:
        """KST 시계 폴링 → liquidate_at 도달 시 청산 + request_stop. 1초 간격.

        run() 의 메인 tick 루프와 별도 task — 장 마감 후 시세가 끊겨도 청산이 도는 게 핵심.
        4/30 launchd 사례에서 시세 끊김으로 7시간 hang 한 버그를 답습하지 않으려면 시계 task 분리 필수.
        """
        if self.liquidate_at is None:
            return
        while True:
            if self._control_state is RunnerControlState.STOPPING:
                return
            now_t = datetime.now(KST).time()
            if now_t >= self.liquidate_at and not self._liquidation_done:
                account = self._account
                if account is None:
                    log.warning("runner.liquidate_skipped", reason="no_account_snapshot")
                else:
                    try:
                        await self.liquidate_all(rest, account, reason="liquidate_at_reached")
                    except Exception as exc:
                        log.error("runner.liquidate_loop_error", error=str(exc), exc_info=exc)
                # 체결 확인용 grace — 너무 짧으면 미체결, 너무 길면 다음날까지 lingering.
                await asyncio.sleep(self.liquidate_grace_sec)
                self.request_stop()
                return
            await asyncio.sleep(1)
