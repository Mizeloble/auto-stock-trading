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
import time as _time
from dataclasses import dataclass, field
from datetime import date as _date
from datetime import datetime, time, timedelta
from decimal import Decimal

import structlog

from .._common import KST, Bar, Side
from ..account.balance import AccountSnapshot, get_balance
from ..account.order_journal import OrderJournal
from ..account.orders import Order, OrderType, place_order
from ..account.recovery import find_suspect_orders
from ..account.reject_classifier import RejectCategory, classify_reject
from ..config import Settings, get_settings
from ..heartbeat import Heartbeat
from ..kiwoom.rest import KiwoomRestClient
from ..kiwoom.schemas import KiwoomError
from ..kiwoom.ws import KiwoomWebSocket, Subscription
from ..market.feed import ticks
from ..monitor.latency import LatencyTracker, get_default_latency_tracker
from ..notify.base import NoopNotifier, Notifier
from ..notify.state import RunnerStateSnapshot
from ..risk.consecutive_loss import ConsecutiveLossTracker
from ..risk.correlation_collector import refresh_correlations
from ..risk.manager import AccountState, Decision, RiskManager
from ..risk.position_guard import GuardAction, PositionGuard
from ..strategy.base import Signal, Strategy
from .aggregator import _BarAggregator
from .aggregator import _floor_bucket as _floor_bucket
from .control import RunnerControlState

log = structlog.get_logger(__name__)


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
    # 29단계 PR2 (#9 2차) — 주문 영속 저널. 미지정이면 저널링 비활성 (테스트·기존 호환).
    # 주입 시 매 place_order 호출에 자동 전달, 시동 시 replay 로 손실 의심 주문 알림.
    journal: OrderJournal | None = None
    # 29단계 PR3 (#13) — 봇 살아있음 신호. 주입 시 background task 가
    # interval_sec 마다 파일 mtime 갱신. 외부 cron 이 누락 시 텔레그램 알림.
    heartbeat: Heartbeat | None = None
    # 29단계 PR10 (#14 2차) — 연속 손절 자동 일시중지. None 이면 비활성.
    # __post_init__ 에서 risk.limits.max_consecutive_losses > 0 면 자동 생성.
    loss_tracker: ConsecutiveLossTracker | None = None
    # 29단계 PR19 (#20) — 주문 송신 latency 모니터. None 이면 비활성.
    # __post_init__ 에서 Settings.latency_log_path 활성 시 자동 생성.
    latency_tracker: LatencyTracker | None = None
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
    # 29단계 PR6 (#10 2차) — reject 분류 결과로 일시 차단된 종목.
    # symbol → skip 만료 시각 (KST). 만료 후 자연 회복.
    _skipped_until: dict[str, datetime] = field(default_factory=dict, init=False)
    # 29단계 PR28 (#15 Phase 2) — 보유 종목 페어 상관계수 캐시.
    # 캐시 키: (refresh_date, frozenset(보유 symbol)). 둘 중 하나 변경 시 재계산.
    # 한도 비활성(max_position_correlation=0) 이면 새로고침 자체를 스킵 — 외부 호출 0.
    _holding_correlations: dict[tuple[str, str], float] = field(default_factory=dict, init=False)
    _correlations_refresh_date: _date | None = field(default=None, init=False)
    _correlations_refresh_symbols: frozenset[str] = field(default_factory=frozenset, init=False)
    # 종목 코드→이름 매핑. 발굴(set_universe) · 잔고 스냅샷의 holdings.name · 호출자
    # 명시 등록을 통해 채워지고, notifier 가 메시지 빌더에 `name=` 으로 같이 전달.
    # 5/14 사용자 피드백: 텔레그램에서 코드만 보면 어떤 종목인지 모르겠다.
    _symbol_names: dict[str, str] = field(default_factory=dict, init=False)
    # 5/14 회귀 — SELL 발주 후 키움 잔고 응답에 체결이 반영되기까지 race.
    # _snapshot_account 가 raw 잔고를 받을 때 이 dict 의 수량만큼 차감해 가드·전략이
    # 잔량을 부풀려 보고 다시 SELL 시도하는(insufficient_balance 800033) 패턴 차단.
    # symbol → (남은 미체결 SELL 수량, 등록 시각). _pending_sell_ttl_sec 이 지나면
    # 자연 만료(키움 잔고를 다시 신뢰). 발주 실패(except) 시에는 등록 안 함.
    _pending_sells: dict[str, tuple[int, datetime]] = field(default_factory=dict, init=False)
    _pending_sell_ttl_sec: int = field(default=180, init=False)
    # 활성 REST 클라이언트 참조 — force_balance_refresh() 가 별도 인스턴스 안 만들고
    # 메인 루프와 같은 토큰·세션을 재사용하기 위함. run() 의 async with 범위에서만 살아있음.
    _rest_client: KiwoomRestClient | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        # 23단계 Phase C — 명시 주입 안 했으면 risk.limits 를 그대로 따르는 PositionGuard 자동 생성.
        if self.position_guard is None:
            self.position_guard = PositionGuard(limits=self.risk.limits)

        # 29단계 PR10 — 연속 손절 트래커 자동 생성 (limits 가 활성일 때만).
        if self.loss_tracker is None and self.risk.limits.max_consecutive_losses > 0:
            self.loss_tracker = ConsecutiveLossTracker(
                threshold=self.risk.limits.max_consecutive_losses,
                pause_seconds=self.risk.limits.consecutive_loss_pause_sec,
            )

        # 29단계 PR19 — latency 트래커 자동 생성 (Settings 의 path 활성 시).
        if self.latency_tracker is None:
            self.latency_tracker = get_default_latency_tracker(self._settings)

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

        # 29단계 회귀(2026-05-11) — 이전 사이클의 "정상 종료" sentinel 정리.
        # cron heartbeat-check 가 다시 미갱신을 잡아낼 수 있게.
        if self.heartbeat is not None:
            self.heartbeat.clear_stopped()

        # 29단계 PR2 — 시동 시 손실 의심 주문 검사. 자동 복구는 X (다음 PR), 알림만.
        await self._warn_recovery_pending()

        # day-trade 모드: orchestrator 가 발굴 후 set_universe() 를 부를 때까지 대기.
        # 그동안 STOPPING 진입(텔레그램 /stop, SIGTERM)은 즉시 빠져나가야 함.
        if not self._universe_ready.is_set():
            log.info("runner.awaiting_universe", liquidate_at=str(self.liquidate_at))
            done = await self._wait_universe_or_stop()
            if not done:
                # universe 가 채워지기 전에 외부 stop. notifier 만 깔끔히 정리하고 종료.
                # 29단계 회귀 — heartbeat 가 주입된 경우 정상 종료 sentinel 도 함께 touch.
                if self.heartbeat is not None:
                    self.heartbeat.mark_stopped()
                await self.notifier.notify("runner.stopped", reason="stopped_before_universe")
                await self.notifier.aclose()
                return

        strat_names = sorted({type(s).__name__ for s in self.strategies.values()})
        # symbol_names 는 텔레그램 메시지에서 코드 옆에 종목명을 합쳐 표시하기 위함.
        # 발굴 단계에 set_universe(names=…) 로 채워졌거나, swing 모드라 비어 있을 수 있음.
        await self.notifier.notify(
            "runner.started",
            strategy=",".join(strat_names),
            symbols=",".join(self.symbols),
            symbol_names={
                s: self._symbol_names[s] for s in self.symbols if s in self._symbol_names
            },
            dry_run=self.dry_run,
        )

        stop_reason = "정상 종료"
        liquidation_task: asyncio.Task[None] | None = None
        heartbeat_task: asyncio.Task[None] | None = None
        try:
            async with KiwoomRestClient(settings=self._settings) as rest:
                # force_balance_refresh() 가 같은 세션을 재사용할 수 있도록 보관.
                self._rest_client = rest
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

                # 29단계 PR3 (#13) — heartbeat 갱신 task. tick 이 끊겨도 봇 살아있다는 증거.
                if self.heartbeat is not None:
                    heartbeat_task = asyncio.create_task(self._heartbeat_loop())

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
                    # 5/14 회귀 — 청산이 발사된 채로 main loop 가 종료되면 self._snapshot 이
                    # 청산 직전 상태로 굳어 텔레그램 일일 리포트가 "보유 N주" 로 stale 표시.
                    # ws 정지 후 짧게 대기하고 한 번 더 잔고를 받아옴. 키움이 미반영 응답을
                    # 줘도 _pending_sells 차감이 보정하므로 holdings 가 0 이 되는 게 정상.
                    if self._liquidation_done:
                        try:
                            await asyncio.sleep(1.0)
                            await self._snapshot_account(rest, previous=account)
                        except Exception as exc:
                            log.warning("runner.final_balance_refresh_failed", error=str(exc))
                    self._rest_client = None
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
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
            # 29단계 회귀(2026-05-11) — 정상 종료 sentinel touch. cron 이 이후
            # missing 알림을 30분마다 쏘지 않게.
            if self.heartbeat is not None:
                self.heartbeat.mark_stopped()
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
        # 잔고 응답에 포함된 종목명을 흡수 — set_universe 발굴 단계에 없던 종목(이전일
        # 보유 잔재 등) 도 텔레그램 메시지에서 코드+이름으로 표시 가능.
        for h in snap.holdings:
            if h.name and h.name.strip():
                self._symbol_names.setdefault(h.symbol, h.name)
        # 5/14 회귀 — 미체결 SELL 만큼 raw 잔고에서 차감. _pending_sells 의 TTL 지난
        # 항목은 만료 (다음 호출에서 키움 응답을 그대로 신뢰).
        self._reap_pending_sells()
        for sym, (pending_qty, _ts) in self._pending_sells.items():
            if pending_qty <= 0:
                continue
            current = positions.get(sym, 0)
            positions[sym] = max(0, current - pending_qty)
        # 29단계 회귀(2026-05-11) — total_equity 는 "예수금 + 보유 평가금" 으로 명시 계산.
        # 키움 `tot_evlt_amt` 는 모드/응답 버전에 따라 보유 평가금만 담는 경우가 있어
        # 이를 그대로 day_start_equity 분모로 쓰면 일부 매도 직후 분자만 줄어든 것처럼
        # 보여 kill switch 가 오발동(예: 12주→부분 익절 6주에서 today_change≈-47% 로 계산)함.
        # 두 필드를 합산하면 매도가 cash↑·equity↓ 로 상쇄돼 안정.
        holdings_value = sum(float(h.market_value) for h in snap.holdings)
        equity = float(snap.deposit) + holdings_value
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
        # 29단계 PR28 — 시그널 평가 전에 페어 상관계수 캐시 갱신 (한도 활성 시만).
        await self._refresh_correlations_if_needed(rest, account)
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
                name=self._name_of(action.symbol),
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
            name=self._name_of(action.symbol),
            reason=action.reason.value,
            qty=action.quantity,
            price=action.price,
            dry_run=False,
        )

    def _is_symbol_skipped(self, symbol: str) -> bool:
        """29단계 PR6 — reject 분류로 등록된 종목이 아직 skip 기간이면 True.

        만료된 항목은 즉시 청소 (다음 호출 비용 0). 동시성 X — runner 단일 task.
        """
        until = self._skipped_until.get(symbol)
        if until is None:
            return False
        if datetime.now(KST) >= until:
            self._skipped_until.pop(symbol, None)
            return False
        return True

    def unpause_loss_tracker(self) -> bool:
        """29단계 PR10 — 텔레그램 `/unpause` (PR11) 등에서 수동 해제.

        반환: 트래커가 활성 + paused 상태였을 때 True (실제로 풀었음).
              비활성/이미 회복 상태면 False.
        """
        if self.loss_tracker is None or not self.loss_tracker.is_paused():
            return False
        self.loss_tracker.unpause()
        log.info("runner.loss_tracker_unpaused")
        return True

    async def _refresh_correlations_if_needed(
        self, rest: KiwoomRestClient, account: AccountState
    ) -> None:
        """보유 종목 또는 날짜 변경 시 페어 상관계수 재계산 — 29단계 PR28.

        무엇/왜:
        - ``max_position_correlation`` 비활성 (0) 이면 외부 호출 자체를 스킵.
        - 캐시 키 = (오늘 KST 날짜, 현재 보유 frozenset). 키 일치하면 재호출 없음.
        - 보유 0 이면 가드 무의미 → 빈 dict 유지.
        - fetch 실패 (전체 또는 일부) 는 warning 로그만 + 마지막 캐시 보존 — BUY 가
          가드 없이 통과하는 부수효과를 최소화.

        어떻게 확인: tests/test_runner_correlation.py.
        """
        if self.risk.limits.max_position_correlation <= 0:
            return
        held = frozenset(s for s, q in account.positions.items() if q > 0)
        if not held:
            self._holding_correlations = {}
            self._correlations_refresh_symbols = frozenset()
            return
        today = datetime.now(tz=KST).date()
        if self._correlations_refresh_date == today and self._correlations_refresh_symbols == held:
            return
        try:
            self._holding_correlations = await refresh_correlations(rest, sorted(held))
        except Exception as exc:
            log.warning("runner.correlation_refresh_failed", error=str(exc))
            return
        self._correlations_refresh_date = today
        self._correlations_refresh_symbols = held
        log.info(
            "runner.correlation_refreshed",
            symbols=sorted(held),
            pairs=len(self._holding_correlations),
        )

    async def _evaluate_signal(
        self, signal: Signal, bar: Bar, account: AccountState
    ) -> Order | None:
        """리스크 평가 + PAUSED/dry-run 게이트.

        None 반환 케이스(각각 로그 한 줄):
        - 종목 skip 중 (직전 reject 분류) → `runner.signal_skipped_reject`
        - 리스크 매니저 REJECT → `runner.signal_rejected`
        - PAUSED → `runner.order_skipped_paused` (텔레그램 /last_signal 정확도 위해 시그널·리스크는 그대로)
        - dry_run → `runner.order_simulated`
        Order 반환 시 호출자가 _place_and_track 으로 실제 송신.
        """
        if self._is_symbol_skipped(signal.symbol):
            until = self._skipped_until[signal.symbol]
            log.info(
                "runner.signal_skipped_reject",
                symbol=signal.symbol,
                side=signal.side.value,
                until=until.isoformat(),
            )
            return None
        # 29단계 PR10 — 연속 손절로 paused 면 신규 BUY 차단. SELL(청산) 은 허용 — 손절은 막지 않음.
        if (
            signal.side is Side.BUY
            and self.loss_tracker is not None
            and self.loss_tracker.is_paused()
        ):
            until_iso = (
                self.loss_tracker.paused_until.isoformat() if self.loss_tracker.paused_until else ""
            )
            log.info(
                "runner.signal_blocked_consecutive_loss",
                symbol=signal.symbol,
                streak=self.loss_tracker.streak,
                paused_until=until_iso,
            )
            return None
        decision = self.risk.evaluate(
            signal,
            price=bar.close,
            account=account,
            holding_correlations=self._holding_correlations or None,
        )
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

    async def _warn_recovery_pending(self) -> None:
        """저널을 replay 해서 결과 미확인 주문이 있으면 텔레그램에 경고.

        29단계 PR2 — 자동 매칭/복구는 다음 PR. 이번 단계는 *발견*만 하고
        사용자가 수동 확인할 수 있게 알림. journal 미주입이면 스킵.
        """
        if self.journal is None:
            return
        suspects = find_suspect_orders(self.journal.replay())
        if not suspects:
            return
        # 시크릿이 새지 않게 client_id 는 앞 8자만, 나머지 표시 정보는 도메인 데이터.
        samples = [f"{s.client_id[:8]} {s.side} {s.symbol} x{s.quantity}" for s in suspects[:5]]
        log.error(
            "runner.recovery_pending",
            count=len(suspects),
            samples=samples,
        )
        await self.notifier.notify(
            "runner.recovery_pending",
            count=len(suspects),
            samples=samples,
        )

    async def _place_and_track(
        self, rest: KiwoomRestClient, order: Order, bar: Bar, account: AccountState
    ) -> None:
        """REST 송신 + 성공·실패 분기. 성공 시 로컬 포지션·평단 즉시 갱신, notifier 양방향."""
        # 29단계 PR19 — 송신부터 응답까지 시간 측정 (활성 시).
        t0 = _time.monotonic()
        try:
            result = await place_order(rest, order, journal=self.journal)
            # 29단계 PR19 — 성공 송신 latency 기록 (활성 시).
            if self.latency_tracker is not None:
                self.latency_tracker.record(
                    label="order_send_ok",
                    duration_ms=(_time.monotonic() - t0) * 1000.0,
                )
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
                name=self._name_of(order.symbol),
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
            elif order.side is Side.SELL:
                # 5/14 회귀 — 다음 _snapshot_account 가 키움 raw 잔고를 받았을 때
                # 체결 미반영분(800033 insufficient_balance) 만큼 빼주도록 등록.
                self._register_pending_sell(order.symbol, order.quantity)
            # 29단계 PR10 (#14 2차) — SELL 시 라운드트립 PnL 산출 → 트래커 갱신.
            # 평균가는 BUY 시에만 갱신되므로 SELL 직전 avg_prices 가 매수가.
            # 부분 매도 시점에도 평균가 그대로 유지 — 누적 라운드트립 측정에 적합.
            if order.side is Side.SELL and self.loss_tracker is not None:
                buy_price = account.avg_prices.get(order.symbol, 0.0)
                if buy_price > 0:
                    pnl = (ref_price - buy_price) * order.quantity
                    was_paused = self.loss_tracker.is_paused()
                    self.loss_tracker.record_round_trip(pnl)
                    # paused 로 *방금* 전환됐으면 텔레그램에 알림.
                    if not was_paused and self.loss_tracker.is_paused():
                        await self.notifier.notify(
                            "risk.consecutive_loss_pause",
                            streak=self.loss_tracker.streak,
                            paused_until=(
                                self.loss_tracker.paused_until.isoformat()
                                if self.loss_tracker.paused_until
                                else ""
                            ),
                        )

            # 23단계 Phase D — SELL 로 잔량 0 이면 last_exit_ts 갱신 + guard reset.
            if order.side is Side.SELL and account.positions.get(order.symbol, 0) <= 0:
                self._mark_exit(order.symbol, bar.ts)
        except Exception as exc:
            # 29단계 PR19 — 실패 송신 latency 도 기록 (timeout/거절 분석).
            if self.latency_tracker is not None:
                self.latency_tracker.record(
                    label="order_send_fail",
                    duration_ms=(_time.monotonic() - t0) * 1000.0,
                )
            # 29단계 PR6 (#10 2차) — KiwoomError 면 카테고리 분류 + skip 등록.
            # 그 외 예외(네트워크 등)는 카테고리 UNKNOWN, skip 0.
            category = RejectCategory.UNKNOWN
            skip_seconds = 0
            if isinstance(exc, KiwoomError):
                classification = classify_reject(exc.message, exc.code)
                category = classification.category
                skip_seconds = classification.skip_seconds
                if skip_seconds > 0:
                    self._skipped_until[order.symbol] = datetime.now(KST) + timedelta(
                        seconds=skip_seconds
                    )
            # 5/14 회귀 — SELL 발주가 매도가능수량 부족으로 실패하는 케이스는
            # in-flight race(아직 _pending_sells 가 적용 안 된 봉) 가 절대다수.
            # 로그는 정상 분류해 남기되 텔레그램 ❌ 알림은 생략 — 사용자에게는
            # 실제 자금/포지션 손실이 아닌 정상 흐름의 잔재.
            silence_notify = (
                category is RejectCategory.INSUFFICIENT_BALANCE and order.side is Side.SELL
            )
            log.error(
                "runner.order_failed",
                error=str(exc),
                symbol=order.symbol,
                category=category.value,
                skip_seconds=skip_seconds,
                notify_silenced=silence_notify,
                exc_info=exc,
            )
            if not silence_notify:
                await self.notifier.notify(
                    "runner.order_failed",
                    error=str(exc),
                    symbol=order.symbol,
                    name=self._name_of(order.symbol),
                    side=order.side.value,
                    qty=order.quantity,
                    category=category.value,
                    skip_seconds=skip_seconds,
                )

    def register_symbol_names(self, names: dict[str, str]) -> None:
        """발굴 결과 등 외부에서 코드→이름 매핑을 주입. 빈 값/공백은 무시."""
        for sym, name in names.items():
            if sym and isinstance(name, str) and name.strip():
                self._symbol_names[sym] = name

    def _name_of(self, symbol: str | None) -> str | None:
        """종목 코드 → 종목명. 없으면 None (notifier 가 코드만 표시)."""
        if not symbol:
            return None
        return self._symbol_names.get(symbol)

    def _register_pending_sell(self, symbol: str, quantity: int) -> None:
        """SELL 발주 직후 _pending_sells 에 미체결 수량 누적. 같은 종목 다회 발주 합산."""
        if quantity <= 0:
            return
        prev, _ = self._pending_sells.get(symbol, (0, datetime.now()))
        self._pending_sells[symbol] = (prev + quantity, datetime.now())

    def _reap_pending_sells(self) -> None:
        """TTL 지난 _pending_sells 엔트리 제거. _pending_sell_ttl_sec 기준."""
        if not self._pending_sells:
            return
        now = datetime.now()
        ttl = timedelta(seconds=self._pending_sell_ttl_sec)
        stale = [sym for sym, (_qty, ts) in self._pending_sells.items() if now - ts > ttl]
        for sym in stale:
            self._pending_sells.pop(sym, None)

    async def force_balance_refresh(self) -> bool:
        """텔레그램 `/balance` 등 외부에서 캐시를 즉시 갱신하도록 트리거.

        5/14 회귀: 운영 중 `/balance` 가 빈 보유종목·총평가 0 으로 떨어진 케이스 —
        키움 mock 잔고 응답의 일시적 race. 핸들러가 응답 직전 호출하면 다음
        포맷 시점에 최신 스냅샷을 사용. 활성 REST 가 없거나(러너 정지 후) 갱신
        실패하면 False — 호출자는 stale 안내로 폴백.
        """
        rest = self._rest_client
        if rest is None:
            return False
        try:
            await self._snapshot_account(rest, previous=self._account)
            return True
        except Exception as exc:
            log.warning("runner.force_balance_refresh_failed", error=str(exc))
            return False

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
            symbol_names=dict(self._symbol_names),
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
        *,
        names: dict[str, str] | None = None,
    ) -> None:
        """orchestrator 가 발굴 결과를 주입할 때 호출.

        무엇/왜:
        - day-trade 모드는 시작 시점에 매수 대상이 미정 — 09:00:30 발굴 후 주입.
        - 호출되면 run() 내부의 `_wait_universe_or_stop()` 가 깨어나 ws.start 진행.
        - 시작 후(ws 활성) 변경은 PR-B 범위 밖 — 시작 전 1회만 허용.
        - names 는 코드→종목명 매핑(옵션) — 텔레그램 알림에서 코드+이름 표시용.
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
        if names:
            self.register_symbol_names(names)
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
                result = await place_order(rest, order, journal=self.journal)
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
                    name=self._name_of(symbol),
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

    async def _heartbeat_loop(self) -> None:
        """heartbeat 파일 mtime 을 interval_sec 간격으로 갱신.

        29단계 PR3 (#13) — run() 의 메인 tick 루프와 분리된 background task 라
        시세가 끊겨도 갱신이 도는 게 핵심. STOPPING 진입 시 즉시 종료.
        시작 직후 1회 즉시 write — 봇 시동 사실 자체를 cron 이 즉시 인지.
        """
        if self.heartbeat is None:
            return
        self.heartbeat.write()
        while self._control_state is not RunnerControlState.STOPPING:
            try:
                await asyncio.sleep(self.heartbeat.interval_sec)
            except asyncio.CancelledError:
                return
            self.heartbeat.write()

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
