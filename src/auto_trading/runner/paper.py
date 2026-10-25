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
from ..account.untradeable import UntradeableStore
from ..config import Settings, get_settings
from ..heartbeat import Heartbeat
from ..kiwoom.rest import KiwoomRestClient
from ..kiwoom.schemas import KiwoomError
from ..kiwoom.ws import KiwoomWebSocket, Subscription
from ..market.feed import ticks
from ..monitor.latency import LatencyTracker, get_default_latency_tracker
from ..monitor.tick_recorder import TickRecorder, get_default_tick_recorder
from ..notify.base import NoopNotifier, Notifier
from ..notify.state import RunnerStateSnapshot
from ..risk.consecutive_loss import ConsecutiveLossTracker
from ..risk.correlation_collector import refresh_correlations
from ..risk.manager import AccountState, Decision, RiskManager
from ..risk.position_guard import GuardAction, PositionGuard
from ..shadow import ShadowSignal, ShadowTracker
from ..strategy.base import Signal, Strategy
from .aggregator import _BarAggregator
from .control import RunnerControlState
from .day_state import DayState, DayStateStore
from .wallclock import WallclockGapDetector

log = structlog.get_logger(__name__)

# churn 브레이크(daily_realized_loss_limit) 의 누적 실현손익을 NET(비용 차감)으로 추정하기
# 위한 비용율. **reports/daily.py 의 COST_* / backtest/broker.py 와 수동 동기화**(import 로
# 안 묶는 이유는 모듈 간 역방향 의존 회피 — reports/CLAUDE.md 의 동기화 규칙과 동일).
# 한쪽만 바꾸면 런타임 브레이크와 사후 리포트 손익이 어긋난다.
_BRAKE_COMMISSION_RATE = 0.00015  # 0.015% (매수+매도)
_BRAKE_TAX_RATE_SELL = 0.0020  # 0.20% (매도)
_BRAKE_SLIPPAGE_RATE = 0.0005  # 5bp (매수+매도)

# day_state 복원 시 입출금 가드 임계 (2026-06-14 적대적 검증). 디스크 baseline 과 현재
# 평가액 괴리가 이 비율을 넘으면 장중 입출금으로 보고 복원 거부 → stale baseline 으로
# kill switch 가 오발동/미발동하는 것을 막는다. 일일 손실 한도(보통 3%)보다 충분히 커
# 정상 일중 손익은 거의 닿지 않고, 큰 괴리는 입출금이 유력.
DAY_STATE_MAX_DRIFT = 0.15


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
    # 청산 실패(네트워크 등) 시 STOPPING 으로 굳기 전 실패분만 재시도하는 횟수/간격.
    # 0 이면 재시도 없이 기존 동작(1회 시도 후 종료). 실패분만 재송신 = idempotent.
    liquidate_max_retries: int = 2
    liquidate_retry_delay_sec: int = 5
    # 29단계 PR2 (#9 2차) — 주문 영속 저널. 미지정이면 저널링 비활성 (테스트·기존 호환).
    # 주입 시 매 place_order 호출에 자동 전달, 시동 시 replay 로 손실 의심 주문 알림.
    journal: OrderJournal | None = None
    # 29단계 PR3 (#13) — 봇 살아있음 신호. 주입 시 background task 가
    # interval_sec 마다 파일 mtime 갱신. 외부 cron 이 누락 시 텔레그램 알림.
    heartbeat: Heartbeat | None = None
    # 2026-06-10 제3자 검토 — 당일 한도 기준(day_start_equity·매수대금·BUY 카운트·churn
    # 누적)을 디스크에 보존. 장중 비정상 재시동 시 한도가 희석되지 않게 같은 날짜면 복원.
    # None 이면 비활성 (테스트·기존 호환).
    day_state: DayStateStore | None = None
    # 2026-06-16 — 종목 단위 거래불가(매매제한·거래정지·무효코드) 이력을 디스크에
    # 누적. 주입 시 발주 거부가 해당 카테고리면 기록 → 다음 거래일 발굴이 사전 회피.
    # None 이면 비활성 (테스트·기존 호환).
    untradeable: UntradeableStore | None = None
    # 29단계 PR10 (#14 2차) — 연속 손절 자동 일시중지. None 이면 비활성.
    # __post_init__ 에서 risk.limits.max_consecutive_losses > 0 면 자동 생성.
    loss_tracker: ConsecutiveLossTracker | None = None
    # 29단계 PR19 (#20) — 주문 송신 latency 모니터. None 이면 비활성.
    # __post_init__ 에서 Settings.latency_log_path 활성 시 자동 생성.
    latency_tracker: LatencyTracker | None = None
    # 2026-07-02 diag — 0B 틱 원본 JSONL 기록(틱 기반 PositionGuard 반사실 분석용
    # 수집). None 이면 비활성. __post_init__ 에서 Settings.tick_log_dir 활성 시
    # 자동 생성. 전략·리스크·주문 경로와 완전 무관 — 버퍼 append 후 묶음 write.
    tick_recorder: TickRecorder | None = None
    # 2026-07-11 — 섀도우 세션(게이트 차단일 주문 없는 가상 매매). enable_shadow() 로
    # 주입되면 dry_run 강제 + 잔고 조회를 가상 원장(tracker)으로 대체한다. 시뮬 체결이
    # tracker 에 쌓여 포지션·평가액이 갱신되므로 손절·트레일링·청산이 실제와 동일하게
    # 돈다(일반 dry_run 은 시뮬 매수가 잔고에 안 잡혀 출구 로직이 못 돌던 것과 대비).
    # 실주문·실계좌·day-state 디스크에는 어떤 영향도 없음.
    shadow_tracker: ShadowTracker | None = None
    # 2026-10-18 — 장중 벽시계 공백(맥북 슬립) 감지. run() 시작 직후부터 도는 독립 task 가
    # 깨어난 직후 공백을 잡아 로그 + 텔레그램(하루 1회). None 이면 비활성.
    wallclock_gap: WallclockGapDetector | None = field(default_factory=WallclockGapDetector)
    _settings: Settings = field(default_factory=get_settings)
    _balance_failures: int = field(default=0, init=False)
    # 텔레그램 인커밍 명령 응답용 인메모리 상태 (runner 가 매 사이클 갱신)
    _snapshot: AccountSnapshot | None = field(default=None, init=False)
    _account: AccountState | None = field(default=None, init=False)
    _recent_signals: dict[str, Signal] = field(default_factory=dict, init=False)
    _started_at: datetime | None = field(default=None, init=False)
    # 종목별 마지막 봉 종가 캐시. 섀도우 tracker.equity() 의 mark 이자, dry-run
    # EOD 청산 시뮬 체결가 근사 — 리포트 실현손익이 청산분을 놓치지 않게 로그에 남긴다.
    _last_closes: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    # 텔레그램 v3 — 외부에서 pause/resume/request_stop 으로 토글
    _control_state: RunnerControlState = field(default=RunnerControlState.RUNNING, init=False)
    # request_stop 시 ws.messages() 루프에 즉시 신호를 주기 위한 활성 ws 참조.
    _ws: KiwoomWebSocket | None = field(default=None, init=False, repr=False)
    # request_stop(reason=…) 로 주입된 종료 사유. universe 대기 중 종료 시 텔레그램
    # 메시지를 사유별(무발굴/발굴실패/외부정지)로 구분해 표시하기 위함.
    _stop_reason: str | None = field(default=None, init=False, repr=False)
    # day-trade 모드: set_universe() 가 채워질 때까지 run() 가 대기하는 이벤트.
    _universe_ready: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    # 청산 1회 가드 — 장 마감 후 시계가 계속 도달 상태라도 두 번 호출되지 않게.
    _liquidation_done: bool = field(default=False, init=False)
    # 청산 *시작* 가드 — liquidate_all 진입 시 True. 청산 SELL 이 in-flight 인 동안
    # (그리고 청산 후 grace 30초 동안) 메인 루프가 신규 주문·guard SELL 을 내면
    # 오버나이트 매수/중복 매도가 된다 (2026-06-10 제3자 검토). 한 번 켜지면 러너
    # 수명 동안 유지 — 청산 후는 봇 종료 수순이라 해제할 이유가 없다.
    _liquidation_started: bool = field(default=False, init=False)
    # dry-run 분기에서 BUY 가 "시뮬 체결" 됐는지 — _evaluate_signal 의 진입 롤백이
    # 시뮬 체결을 거절로 오인해 전략 상태를 되돌리지 않게 하는 내부 플래그.
    _eval_buy_simulated: bool = field(default=False, init=False)
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
    # 2026-06-10 제3자 검토 — _pending_sells 의 BUY 대칭. BUY 접수 후 키움 잔고 응답에
    # 체결이 반영되기 전 스냅샷이 오면 보유가 0 으로 되돌아가 (a) 종목 비중 한도를 한 번
    # 더 풀로 쓰는 중복 매수, (b) PositionGuard 보호 공백이 생긴다. symbol → (접수 직후
    # 로컬 기대 총수량, 등록 시각). 키움 raw 가 기대치에 도달하면 해제, TTL 만료 시 폐기.
    _pending_buys: dict[str, tuple[int, datetime]] = field(default_factory=dict, init=False)
    # 회전율 가드 — 종목당 하루 BUY 체결 카운터. risk.limits.max_buys_per_symbol 활성 시만 의미.
    # 같은 종목 반복 진입으로 비용만 누적되는 패턴 차단. 날짜 바뀌면 자동 리셋.
    _daily_buy_counts: dict[str, int] = field(default_factory=dict, init=False)
    _daily_buy_counts_date: _date | None = field(default=None, init=False)
    # 일일 한도 기준(day_start_equity·today_notional_used)이 속한 KST 날짜.
    # BUY 카운트(_roll_daily_buy_counts)와 달리 이 둘은 previous 승계로만 이어져
    # 자정을 넘어도 리셋되지 않던 비대칭(멀티데이 swing 세션에서 한도 누적 소진 +
    # kill switch 기준일 고착)을 _snapshot_account 가 이 필드로 감지해 재설정한다.
    _day_baseline_date: _date | None = field(default=None, init=False)
    # churn 브레이크 — 그날 누적 실현손익(NET=비용 차감, 음수면 손실). SELL 마다 적립, 날짜
    # 바뀌면 _roll_daily_buy_counts 가 함께 리셋. risk.limits.daily_realized_loss_limit 활성 시만 의미.
    _daily_realized_net: float = field(default=0.0, init=False)
    # 안전 브레이크 무방비 경고 — daily_loss_kill 이 켜졌는데 평가금액 기준이 없어
    # kill switch 가 판정 불능일 때 *1회만* 경고/알림(매 잔고 갱신마다 스팸 방지).
    _kill_switch_blind_warned: bool = field(default=False, init=False)
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

        # 2026-07-02 diag — 틱 레코더 자동 생성 (Settings 의 dir 활성 시).
        if self.tick_recorder is None:
            self.tick_recorder = get_default_tick_recorder(self._settings)

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

        # 벽시계 공백 감시는 universe 대기 구간(발굴 전)까지 덮어야 하므로 세션 본문
        # 바깥에서 시동한다 — 슬립은 종목이 정해지기 전에도 수집을 망가뜨린다.
        gap_task: asyncio.Task[None] | None = None
        if self.wallclock_gap is not None:
            gap_task = asyncio.create_task(self._wallclock_gap_loop(self.wallclock_gap))
        try:
            await self._run_session()
        finally:
            if gap_task is not None:
                gap_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await gap_task

    async def _run_session(self) -> None:
        self._install_signal_handlers()
        self._started_at = datetime.now()

        # 29단계 회귀(2026-05-11) — 이전 사이클의 "정상 종료" sentinel 정리.
        # cron heartbeat-check 가 다시 미갱신을 잡아낼 수 있게.
        #
        # 2026-06-01 회귀 — clear_stopped() 직후 곧바로 heartbeat 를 1회 기록한다.
        # 안 그러면 시동(clear_stopped)과 종료 sentinel(mark_stopped) 사이 구간에
        # runner.txt 가 *지난 거래일* 값으로 남는다. breadth 게이트로 거래 없이
        # 일찍 종료하는 날엔 매매 루프(=heartbeat_loop)에 못 들어가 이 구간이
        # 그대로 노출되고, 5분 cron 이 끼면 "stale = 봇 멈춤" 가짜 경보가 뜬다.
        # 시동 시점 1회 write 로 그 구간의 runner.txt 를 항상 fresh 로 유지.
        if self.heartbeat is not None:
            self.heartbeat.clear_stopped()
            self.heartbeat.write()

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
                await self.notifier.notify(
                    "runner.stopped", reason=self._stop_reason or "stopped_before_universe"
                )
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
        protection_task: asyncio.Task[None] | None = None
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

                for strat in set(self.strategies.values()):
                    strat.reset()

                # day-trade 모드면 청산 시계 task 백그라운드로. tick 이 안 와도 청산이 도는 게 핵심.
                if self.liquidate_at is not None:
                    liquidation_task = asyncio.create_task(self._liquidation_loop(rest))

                # 29단계 PR3 (#13) — heartbeat 갱신 task. tick 이 끊겨도 봇 살아있다는 증거.
                if self.heartbeat is not None:
                    heartbeat_task = asyncio.create_task(self._heartbeat_loop())

                # 2026-06-10 제3자 검토 — 잔고 갱신 + kill switch 를 tick 루프에서 분리.
                # 종전엔 tick 도착에 묶여 있어 WS 재접속 백오프·전 종목 거래정지 동안
                # daily_loss_kill 까지 같이 멈췄다 (포지션 보유 + 시세 단절 + 급락이
                # 겹치면 보호 공백). REST 는 WS 와 무관하게 살아있으므로 독립 task 로.
                protection_task = asyncio.create_task(self._protection_loop(rest))

                try:
                    async for tick in ticks(ws):
                        # === diag: 틱 원본 기록 ===
                        # 버퍼 append 만 — 디스크 write 는 임계/간격 도달 시 묶음.
                        # 트레이딩 판단에 어떤 영향도 없다 (반사실 분석용 수집).
                        if self.tick_recorder is not None:
                            self.tick_recorder.record(tick)

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
                        # protection task 가 주기적으로 self._account 를 갈아끼우므로
                        # 매 봉마다 최신 스냅샷을 읽는다 (stale 로컬 변수 고착 방지).
                        account = self._account or account
                        await self._on_bar(rest, bar, account)
                finally:
                    self._ws = None
                    # diag — 틱 버퍼 잔여분 flush (best-effort, 오류 swallow).
                    if self.tick_recorder is not None:
                        self.tick_recorder.flush()
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
            if protection_task is not None:
                protection_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await protection_task
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
        청산은 `_liquidate_with_retries` 경유 — 급락일 + 일시적 네트워크 오류 조합에서
        포지션을 든 채 종료되지 않게 liquidate_at 경로와 같은 재시도를 적용.
        """
        # fail-loud — kill switch 가 켜졌는데 평가금액 기준을 못 읽어 무방비면, 조용히
        # 넘기지 말고 *1회* 경고/알림(매 갱신마다 스팸 방지). 임계·청산 로직은 불변.
        if self.risk.kill_switch_baseline_missing(account):
            if not self._kill_switch_blind_warned:
                self._kill_switch_blind_warned = True
                log.warning(
                    "runner.kill_switch_blind",
                    day_start_equity=account.day_start_equity,
                    total_equity=account.total_equity,
                )
                await self.notifier.notify("runner.kill_switch_blind")
            return
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
        await self._liquidate_with_retries(rest, account, reason="daily_loss_kill")
        self.request_stop()

    async def _snapshot_account(
        self,
        rest: KiwoomRestClient,
        *,
        previous: AccountState | None,
    ) -> AccountState:
        # 섀도우 세션 — 실잔고 대신 가상 원장. REST 호출·day-state 디스크 접근 없음.
        if self.shadow_tracker is not None:
            return self._shadow_account_state(previous)
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
        # 5/14·6/10 회귀 — in-flight 주문(미체결 SELL·미반영 BUY)에 맞춰 raw 잔고를 보정.
        self._apply_pending_corrections(positions, avg_prices, previous)
        # 29단계 회귀(2026-05-11) — total_equity 는 "예수금 + 보유 평가금" 으로 명시 계산.
        # 키움 `tot_evlt_amt` 는 모드/응답 버전에 따라 보유 평가금만 담는 경우가 있어
        # 이를 그대로 day_start_equity 분모로 쓰면 일부 매도 직후 분자만 줄어든 것처럼
        # 보여 kill switch 가 오발동(예: 12주→부분 익절 6주에서 today_change≈-47% 로 계산)함.
        # 두 필드를 합산하면 매도가 cash↑·equity↓ 로 상쇄돼 안정.
        holdings_value = sum(float(h.market_value) for h in snap.holdings)
        equity = float(snap.deposit) + holdings_value
        today = datetime.now(KST).date()
        day_start = previous.day_start_equity if previous and previous.day_start_equity else equity
        # 23단계 Phase B — 잔고 갱신은 누적 매수대금을 리셋하지 않음 (같은 날 한정).
        today_notional = previous.today_notional_used if previous is not None else 0.0
        # KST 자정 경계 — swing `paper` 세션처럼 며칠 연속 가동하면 previous 승계가
        # 날짜를 넘어 이어진다. BUY 카운트(_roll_daily_buy_counts)처럼 일일 기준도
        # 오늘로 재설정: baseline=현재 평가액, 매수대금 누적=0.
        if (
            previous is not None
            and self._day_baseline_date is not None
            and self._day_baseline_date != today
        ):
            log.info(
                "runner.day_baseline_rolled",
                previous_date=self._day_baseline_date.isoformat(),
                new_date=today.isoformat(),
                old_day_start=round(day_start),
                new_day_start=round(equity),
                released_notional=round(today_notional),
            )
            day_start = equity
            today_notional = 0.0
        # 2026-06-10 제3자 검토 — 첫 스냅샷이면 같은 날짜의 디스크 저장분을 복원.
        # 장중 비정상 재시동 시 baseline 이 재시작 시점 평가액으로 잡혀 일일 손실
        # 한도가 희석되던 문제 (예: -2.5% 손실 후 재시동 → 추가 -3% 까지 허용).
        if previous is None:
            restored = self._restore_day_state(equity)
            if restored is not None:
                if restored.day_start_equity > 0:
                    day_start = restored.day_start_equity
                today_notional = restored.today_notional_used
        self._day_baseline_date = today
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
        self._persist_day_state(state)
        return state

    def _apply_pending_corrections(
        self,
        positions: dict[str, int],
        avg_prices: dict[str, float],
        previous: AccountState | None,
    ) -> None:
        """raw 키움 잔고(positions/avg_prices)를 in-flight 주문 기대치로 in-place 보정.

        키움 잔고 응답은 방금 접수한 주문을 아직 반영 못 할 수 있어, 로컬이 추적하는
        미체결 SELL·미반영 BUY 로 raw 를 보정한다(둘 다 TTL 만료 시 키움 응답을 신뢰).
        """
        # 5/14 회귀 — 미체결 SELL 만큼 raw 잔고에서 차감. _pending_sells 의 TTL 지난
        # 항목은 만료 (다음 호출에서 키움 응답을 그대로 신뢰).
        self._reap_pending_sells()
        for sym, (pending_qty, _ts) in self._pending_sells.items():
            if pending_qty <= 0:
                continue
            current = positions.get(sym, 0)
            positions[sym] = max(0, current - pending_qty)
        # 2026-06-10 제3자 검토 — BUY 대칭 보정. 접수된 매수가 키움 잔고에 아직 미반영이면
        # 로컬 기대 수량을 유지 — 안 그러면 보유가 0 으로 되돌아가 같은 종목 비중 한도를
        # 한 번 더 쓰는 중복 매수 + PositionGuard 보호 공백이 생긴다. raw 가 기대치에
        # 도달하면 반영 완료로 보고 해제 (TTL 만료 시에는 키움 응답을 다시 신뢰).
        self._reap_pending_buys()
        for sym, (expected_qty, _ts) in list(self._pending_buys.items()):
            if positions.get(sym, 0) >= expected_qty:
                self._pending_buys.pop(sym, None)
                continue
            positions[sym] = expected_qty
            if avg_prices.get(sym, 0.0) <= 0 and previous is not None:
                prev_avg = previous.avg_prices.get(sym, 0.0)
                if prev_avg > 0:
                    avg_prices[sym] = prev_avg  # guard 손절 기준 유지 (주문 시 로컬 평단)

    async def _on_bar(self, rest: KiwoomRestClient, bar: Bar, account: AccountState) -> None:
        log.debug("runner.bar", symbol=bar.symbol, ts=bar.ts.isoformat(), close=bar.close)
        self._last_closes[bar.symbol] = bar.close
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
            await self._place_and_track(rest, order, bar, account, signal=signal)

        # 23단계 Phase C — 매 봉 끝에 PositionGuard 체크. 보유 0 이면 자동 no-op.
        await self._run_position_guard(rest, bar, account)

    async def _run_position_guard(
        self, rest: KiwoomRestClient, bar: Bar, account: AccountState
    ) -> None:
        """guard 활성 + 해당 종목 보유 시 stop/trailing/partial 결정 → 즉시 SELL 송신."""
        guard = self.position_guard
        if guard is None or not guard.is_active:
            return
        # 일일 청산이 시작됐으면 guard SELL 도 중단 — 청산 루프가 이미 같은 잔량을
        # 시장가로 내보내는 중이라 여기서 또 내면 중복 매도(800033)가 된다.
        if self._liquidation_started:
            return
        qty = account.positions.get(bar.symbol, 0)
        if qty <= 0:
            return
        # 5/14 회귀 후속(2026-05-16) — 직전 봉들에서 발주돼 아직 키움 잔고에 반영
        # 안 된 SELL 수량을 차감. _snapshot_account 가 raw 잔고는 보정하지만 캐시된
        # account 를 그대로 받은 봉에서는 stale qty 로 guard 가 또 발화 → 같은
        # 포지션을 중복 청산(800033 insufficient_balance) 했다. 미체결 SELL 이 잔량
        # 전체를 덮으면 발화 자체를 막는다(오버셀 방지).
        self._reap_pending_sells()
        pending_qty, _ = self._pending_sells.get(bar.symbol, (0, None))
        net_qty = qty - max(0, pending_qty)
        if net_qty <= 0:
            log.info(
                "position_guard.skipped_inflight_sell",
                symbol=bar.symbol,
                position_qty=qty,
                pending_sell_qty=pending_qty,
            )
            return
        avg = account.avg_prices.get(bar.symbol, 0.0)
        if avg <= 0:
            return
        action = guard.check(symbol=bar.symbol, price=bar.close, quantity=net_qty, avg_price=avg)
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
            # 섀도우 — guard SELL 을 가상 원장에 반영(FIFO 실현손익)하고, 실세션과
            # 동일하게 churn 브레이크·연속손절 트래커에도 적립한다. 이 회로가 같이
            # 돌아야 "실세션이었다면 오후에 진입이 막혔을지"까지 반사실이 재현된다.
            if self.shadow_tracker is not None:
                self.shadow_tracker.apply(
                    ShadowSignal(
                        symbol=action.symbol,
                        side=Side.SELL,
                        quantity=action.quantity,
                        price=action.price,
                        ts=bar.ts,
                    )
                )
                await self._record_sell_round_trip(account, order, ref_price=action.price, bar=bar)
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
        """리스크 평가 + PAUSED/dry-run 게이트. 실제 판정은 _apply_signal_gates.

        BUY 가 거절돼 주문이 안 나갔으면(dry-run 시뮬 체결 제외) 전략의 진입
        기록을 롤백한다 — index_daytrade 류 전략은 시그널 발사 시점에 '보유 중'
        으로 기록하므로, 그대로 두면 거절 한 번에 그 종목이 하루 종일 잠긴다
        (2026-06-10 제3자 검토).
        """
        self._eval_buy_simulated = False
        order = await self._apply_signal_gates(signal, bar, account)
        if order is None and signal.side is Side.BUY and not self._eval_buy_simulated:
            self._rollback_entry_state(signal.symbol)
        return order

    def _rollback_entry_state(self, symbol: str) -> None:
        """체결되지 않은 BUY 의 전략 진입 기록(last_entry_ts) 해제 — 재진입 허용.

        last_exit_ts 는 보존 — 직전 청산 쿨다운은 이미 충족된 상태에서만 새 진입
        시그널이 나오므로 entry 만 지워도 쿨다운 우회는 발생하지 않는다.
        last_entry_ts 를 안 쓰는 전략(sma_crossover 등)은 자연히 no-op.
        """
        strat = self.strategies.get(symbol)
        if strat is None:
            return
        last_entry = strat.state.get("last_entry_ts")
        if last_entry is None:
            return
        last_exit = strat.state.get("last_exit_ts")
        if last_exit is not None and last_entry <= last_exit:
            return  # 이번 시그널이 기록한 진입이 아님 (이미 청산 후 상태)
        strat.state["last_entry_ts"] = None

    async def _apply_signal_gates(
        self, signal: Signal, bar: Bar, account: AccountState
    ) -> Order | None:
        """리스크 평가 + PAUSED/dry-run 게이트.

        None 반환 케이스(각각 로그 한 줄):
        - 일일 청산 시작 후 → `runner.signal_blocked_liquidation`
        - 종목 skip 중 (직전 reject 분류) → `runner.signal_skipped_reject`
        - 리스크 매니저 REJECT → `runner.signal_rejected`
        - PAUSED → `runner.order_skipped_paused` (텔레그램 /last_signal 정확도 위해 시그널·리스크는 그대로)
        - dry_run → `runner.order_simulated`
        Order 반환 시 호출자가 _place_and_track 으로 실제 송신.
        """
        # 일일 청산이 시작됐으면 신규 주문 전면 차단 — BUY 는 오버나이트 포지션이
        # 되고(grace 30초 사이 체결 후 봇 종료), SELL 은 청산 루프와 중복 매도.
        if self._liquidation_started:
            log.info(
                "runner.signal_blocked_liquidation",
                symbol=signal.symbol,
                side=signal.side.value,
            )
            return None
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
        # fail-safe — 안전 브레이크(daily_loss_kill)가 평가금액을 못 읽어 무방비면 새 진입을
        # 자동 차단(사람 대응 없이 위험 비축). 보유분 손절·트레일링·청산(SELL)은 영향 없음 —
        # 이 게이트는 BUY 경로 안에서만 동작. 평가금액이 복구되면 자동으로 진입 재개.
        if signal.side is Side.BUY and self.risk.kill_switch_baseline_missing(account):
            log.warning("runner.buy_blocked_kill_switch_blind", symbol=signal.symbol)
            return None
        # 회전율 가드 — 종목당 일일 BUY 한도 초과 시 신규 진입 차단 (SELL 영향 없음).
        if signal.side is Side.BUY:
            exceeded, count, limit = self._buy_limit_exceeded(signal.symbol)
            if exceeded:
                log.info(
                    "runner.signal_blocked_buy_limit",
                    symbol=signal.symbol,
                    count=count,
                    limit=limit,
                )
                return None
        # churn 브레이크 — 그날 누적 실현손실이 한도 도달 시 신규 진입 자동 중단 (SELL 불변).
        # 손익 기준이라 잘 번 날은 안 막고 손실 쌓인 날만 막는다. 0=비활성.
        if signal.side is Side.BUY:
            blocked, realized_loss, loss_cap = self._daily_loss_limit_exceeded()
            if blocked:
                log.info(
                    "runner.signal_blocked_daily_loss",
                    symbol=signal.symbol,
                    realized_loss=round(realized_loss),
                    limit=round(loss_cap),
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
                # 데이터 기반 분석용 — 진입 사유(전략이 채운 Signal.reason). 사후 리포트가
                # "왜 이 종목을 샀나"를 매수 레코드에 박제(daily.TradeRecord.reason).
                reason=signal.reason or None,
            )
            # 23단계 Phase B — dry-run BUY 도 일일 누적에 포함 (한도 검증 회로 똑같이 흘러야).
            if order.side is Side.BUY:
                # 시뮬 체결 = 진입 성공 — _evaluate_signal 의 진입 롤백 대상 아님.
                self._eval_buy_simulated = True
                account.today_notional_used += sim_price * order.quantity
                self._roll_daily_buy_counts()
                self._daily_buy_counts[order.symbol] = (
                    self._daily_buy_counts.get(order.symbol, 0) + 1
                )
            # 섀도우 — 시뮬 체결을 가상 원장에 기록 + account 에 즉시 반영. 잔고를
            # 안 읽는 섀도우에서는 이게 유일한 포지션 소스라, 반영해야 같은 봉부터
            # PositionGuard(손절·트레일링)와 15:15 청산이 실제처럼 돈다.
            if self.shadow_tracker is not None:
                self.shadow_tracker.apply(
                    ShadowSignal(
                        symbol=order.symbol,
                        side=order.side,
                        quantity=order.quantity,
                        price=sim_price,
                        ts=bar.ts,
                    )
                )
                account.positions[order.symbol] = self.shadow_tracker.holdings.get(order.symbol, 0)
                avg = self.shadow_tracker.avg_prices.get(order.symbol)
                if avg is not None:
                    account.avg_prices[order.symbol] = avg
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
        self,
        rest: KiwoomRestClient,
        order: Order,
        bar: Bar,
        account: AccountState,
        *,
        signal: Signal | None = None,
    ) -> None:
        """REST 송신 + 성공·실패 분기. 성공 시 로컬 포지션·평단 즉시 갱신, notifier 양방향.

        signal 은 전략 발 진입 경로에서만 전달된다(guard SELL 은 None). 진입 사유를
        order_accepted 로그에 남겨 사후 리포트가 매수 레코드에 박제하도록 한다.
        """
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
                reason=(signal.reason or None) if signal is not None else None,
            )
            await self.notifier.notify(
                "runner.order_accepted",
                order_no=result.order_no,
                symbol=order.symbol,
                name=self._name_of(order.symbol),
                side=order.side.value,
                qty=order.quantity,
                # 2026-06-15 — 체결 알림에 단가·총 거래대금 표시용(시장가는 봉 종가 근사).
                price=ref_price,
            )
            # 로컬 포지션 즉시 갱신 (다음 snapshot 까지의 공백 메움)
            prev_qty = account.positions.get(order.symbol, 0)
            delta = order.quantity if order.side is Side.BUY else -order.quantity
            account.positions[order.symbol] = max(0, prev_qty + delta)
            if order.side is Side.BUY:
                self._apply_buy_fill(account, order, ref_price=ref_price, prev_qty=prev_qty)
            elif order.side is Side.SELL:
                # 5/14 회귀 — 다음 _snapshot_account 가 키움 raw 잔고를 받았을 때
                # 체결 미반영분(800033 insufficient_balance) 만큼 빼주도록 등록.
                self._register_pending_sell(order.symbol, order.quantity)
                # 로컬 보유가 줄었으니 BUY 기대치는 폐기 — raw-stale 보정은 pending_sells 담당.
                self._pending_buys.pop(order.symbol, None)
                await self._record_sell_round_trip(account, order, ref_price=ref_price, bar=bar)
            # 2026-06-10 — 한도 기준값(매수대금·BUY 카운트·churn 누적) 디스크 보존.
            # 비정상 재시동 시 _restore_day_state 가 같은 날짜면 복원.
            self._persist_day_state(account)
        except Exception as exc:
            await self._handle_order_error(order, exc, t0=t0)

    def _apply_buy_fill(
        self, account: AccountState, order: Order, *, ref_price: float, prev_qty: int
    ) -> None:
        """BUY 체결 직후 로컬 상태 갱신 — 가중평균·일일 매수대금·회전 카운트·pending 보정."""
        # 추가 매수는 가중평균 — 마지막 매수가로 덮어쓰면 다음 잔고 갱신까지
        # guard 의 손절 기준이 틀어진다. (키움 잔고 갱신 시 정확값으로 복원됨.)
        prev_avg = account.avg_prices.get(order.symbol, 0.0)
        if prev_qty > 0 and prev_avg > 0:
            total_qty = prev_qty + order.quantity
            account.avg_prices[order.symbol] = (
                prev_avg * prev_qty + ref_price * order.quantity
            ) / total_qty
        else:
            account.avg_prices[order.symbol] = ref_price
        # 23단계 Phase B — 일일 매수대금 누적. 실패 시(except 분기) 누적 X.
        account.today_notional_used += ref_price * order.quantity
        # 회전율 가드 — 같은 종목 BUY 체결 카운트 누적. 실패 시(except) 누적 X.
        self._roll_daily_buy_counts()
        self._daily_buy_counts[order.symbol] = self._daily_buy_counts.get(order.symbol, 0) + 1
        # 5/14 회귀 후속(2026-05-16) — 재진입. 직전 라운드트립의 미체결
        # SELL 잔재는 새 포지션과 무관하다. 그대로 두면 _snapshot_account 가
        # 새 보유분까지 차감하고 guard 가 새 포지션을 보호 못 한다(TTL 180s >
        # 일중 회전 주기). 같은 종목 BUY 체결 시 stale pending 을 폐기.
        self._pending_sells.pop(order.symbol, None)
        # 2026-06-10 — BUY 대칭 보정 등록. 다음 잔고 응답이 체결 미반영이어도
        # 로컬 기대 총수량을 유지해 보호 공백·중복 비중 사용을 막는다.
        self._pending_buys[order.symbol] = (
            account.positions[order.symbol],
            datetime.now(),
        )

    async def _record_sell_round_trip(
        self, account: AccountState, order: Order, *, ref_price: float, bar: Bar
    ) -> None:
        """SELL 체결 직후 라운드트립 PnL 산출 → churn 누적·연속손절 트래커·잔량0 guard reset."""
        # 29단계 PR10 (#14 2차) — SELL 시 라운드트립 PnL 산출 → 트래커 갱신.
        # 평균가는 BUY 시에만 갱신되므로 SELL 직전 avg_prices 가 매수가.
        # 부분 매도 시점에도 평균가 그대로 유지 — 누적 라운드트립 측정에 적합.
        buy_price = account.avg_prices.get(order.symbol, 0.0)
        if buy_price > 0:
            pnl = (ref_price - buy_price) * order.quantity
            # churn 브레이크 — 그날 누적 실현손익(NET, 비용 차감)에 적립(일자 경계면
            # 함께 리셋). 비용까지 빼야 검증대로 "잘 번 날은 보존, 손실 쌓인 날만 차단"
            # 이 성립(GROSS 는 승자 재진입까지 잘라 부적합 — 2026-05-31 ledger 검증).
            # daily_realized_loss_limit 활성 시 매수 게이트가 이 값을 읽어 차단.
            buy_notional = buy_price * order.quantity
            sell_notional = ref_price * order.quantity
            cost = buy_notional * (
                _BRAKE_COMMISSION_RATE + _BRAKE_SLIPPAGE_RATE
            ) + sell_notional * (
                _BRAKE_COMMISSION_RATE + _BRAKE_TAX_RATE_SELL + _BRAKE_SLIPPAGE_RATE
            )
            self._roll_daily_buy_counts()
            self._daily_realized_net += pnl - cost
            if self.loss_tracker is not None:
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
        if account.positions.get(order.symbol, 0) <= 0:
            self._mark_exit(order.symbol, bar.ts)

    async def _handle_order_error(self, order: Order, exc: Exception, *, t0: float) -> None:
        """주문 송신 실패 처리 — latency 기록·거절 분류·skip 등록·진입 롤백·로그/알림."""
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
            # 종목 단위 거래불가(매매제한·거래정지·무효코드)는 다음 거래일 발굴이
            # 사전 회피하도록 디스크에 누적. 휴장(HOLIDAY)·잔고부족은 종목 탓이
            # 아니므로 제외 — 시장/계좌 사유라 영속화하면 멀쩡한 종목을 묻는다.
            if self.untradeable is not None and category in (
                RejectCategory.TRADING_HALT,
                RejectCategory.INVALID_SYMBOL,
            ):
                self.untradeable.record(
                    order.symbol,
                    today=datetime.now(KST).date(),
                    name=self._name_of(order.symbol) or "",
                    reason=category.value,
                )
        # 5/14 회귀 — SELL 발주가 매도가능수량 부족으로 실패하는 케이스는
        # in-flight race(아직 _pending_sells 가 적용 안 된 봉) 가 절대다수.
        # 로그는 정상 분류해 남기되 텔레그램 ❌ 알림은 생략 — 사용자에게는
        # 실제 자금/포지션 손실이 아닌 정상 흐름의 잔재.
        silence_notify = category is RejectCategory.INSUFFICIENT_BALANCE and order.side is Side.SELL
        # 발주 실패한 BUY — 전략의 진입 기록을 롤백해 재진입 잠김 방지.
        if order.side is Side.BUY:
            self._rollback_entry_state(order.symbol)
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

    def _roll_daily_buy_counts(self) -> None:
        """KST 자정 경계를 넘어가면 일일 BUY 카운터를 리셋.

        무엇/왜:
        - day-trade 는 매 거래일 새로 시작 — 어제 카운트가 오늘 한도를 갉아먹지 않게.
        - 러너는 일반적으로 매 거래일 launchd 가 새로 띄우지만, 자동 청산 후
          재시동·연장 운용 시나리오까지 안전하게 잡으려고 명시 리셋한다.
        """
        today = datetime.now(KST).date()
        if self._daily_buy_counts_date != today:
            if self._daily_buy_counts:
                log.info(
                    "runner.daily_buy_counts_reset",
                    previous_date=(
                        self._daily_buy_counts_date.isoformat()
                        if self._daily_buy_counts_date
                        else None
                    ),
                    new_date=today.isoformat(),
                )
            self._daily_buy_counts = {}
            self._daily_buy_counts_date = today
            # churn 브레이크 누적도 같은 일자 경계로 리셋.
            self._daily_realized_net = 0.0

    def _buy_limit_exceeded(self, symbol: str) -> tuple[bool, int, int]:
        """BUY 한도 초과 여부 + (현재 카운트, 한도) 반환. 한도 0(비활성) 이면 False."""
        limit = int(self.risk.limits.max_buys_per_symbol or 0)
        if limit <= 0:
            return False, 0, 0
        self._roll_daily_buy_counts()
        count = self._daily_buy_counts.get(symbol, 0)
        return count >= limit, count, limit

    def _daily_loss_limit_exceeded(self) -> tuple[bool, float, float]:
        """그날 누적 실현손실이 한도 도달 여부 + (손실액, 한도) 반환. 한도 0(비활성) 이면 False.

        손실은 양수로 환산(`-_daily_realized_net`). 누적이 이익이면 손실<=0 → 차단 안 함.
        """
        cap = float(self.risk.limits.daily_realized_loss_limit or 0.0)
        if cap <= 0:
            return False, 0.0, 0.0
        self._roll_daily_buy_counts()
        realized_loss = -self._daily_realized_net
        return realized_loss >= cap, realized_loss, cap

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

    def _reap_pending_buys(self) -> None:
        """TTL 지난 _pending_buys 제거 — 만료 후엔 키움 잔고를 다시 신뢰 (SELL 과 동일 TTL)."""
        if not self._pending_buys:
            return
        now = datetime.now()
        ttl = timedelta(seconds=self._pending_sell_ttl_sec)
        stale = [sym for sym, (_qty, ts) in self._pending_buys.items() if now - ts > ttl]
        for sym in stale:
            self._pending_buys.pop(sym, None)

    def _restore_day_state(self, current_equity: float) -> DayState | None:
        """비정상 재시동 복원 — 같은 KST 날짜의 저장분이 있으면 한도 기준값을 되살린다.

        baseline(day_start_equity)·매수대금 누적은 호출자(_snapshot_account)가 적용,
        BUY 카운트·churn 누적(NET)은 여기서 직접 복원. day_state 미주입이면 no-op.

        입출금 가드 (2026-06-14 적대적 검증): 디스크 baseline 과 현재 평가액의 괴리가
        정상 일중 손익 범위(DAY_STATE_MAX_DRIFT)를 넘으면 *장중 입출금* 으로 보고
        복원을 거부한다. 입출금은 equity 총액을 바꿔 stale baseline 을 만들고, kill
        switch(today_change=equity/day_start-1)를 출금 시 오발동(가짜 손실로 전량
        청산)·입금 시 미발동(보호 공백)시킨다. 키움 입출금내역 TR 이 없어 직접
        감지 불가 → 괴리 휴리스틱으로 보수적으로 차단(거부 시 현재 평가액이 baseline).
        일일 손실 한도(보통 3%)에서 이미 청산·정지하므로 정상 손익이 임계(15%)에
        닿는 일은 사실상 없다 — 임계를 넘는 큰 괴리는 입출금이 압도적으로 유력.
        """
        if self.day_state is None:
            return None
        today = datetime.now(KST).date()
        saved = self.day_state.load(today)
        if saved is None:
            return None
        if saved.day_start_equity > 0:
            drift = abs(current_equity / saved.day_start_equity - 1.0)
            if drift > DAY_STATE_MAX_DRIFT:
                log.warning(
                    "runner.day_state_drift_rejected",
                    saved_baseline=round(saved.day_start_equity),
                    current_equity=round(current_equity),
                    drift_pct=round(drift * 100, 1),
                    threshold_pct=round(DAY_STATE_MAX_DRIFT * 100, 1),
                )
                return None
        self._daily_buy_counts = dict(saved.daily_buy_counts)
        self._daily_buy_counts_date = today
        self._daily_realized_net = saved.daily_realized_net
        log.info(
            "runner.day_state_restored",
            day_start_equity=saved.day_start_equity,
            today_notional_used=round(saved.today_notional_used),
            buy_counts=sum(saved.daily_buy_counts.values()),
            daily_realized_net=round(saved.daily_realized_net),
        )
        return saved

    def _persist_day_state(self, account: AccountState) -> None:
        """한도 기준값을 디스크에 저장 — 잔고 갱신·주문 체결 직후 호출. 실패는 swallow."""
        if self.day_state is None:
            return
        self.day_state.save(
            DayState(
                date=datetime.now(KST).date().isoformat(),
                day_start_equity=account.day_start_equity,
                today_notional_used=account.today_notional_used,
                daily_buy_counts=dict(self._daily_buy_counts),
                daily_realized_net=self._daily_realized_net,
            )
        )

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
            shadow=self.shadow_tracker is not None,
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

    def request_stop(self, reason: str | None = None) -> None:
        """graceful 종료 요청. 한 번 진입하면 되돌릴 수 없음.

        ``reason`` 은 universe 대기 중 종료 시 텔레그램 메시지를 사유별로
        구분하기 위한 선택 코드(예: ``no_candidates`` / ``discover_failed``).
        본 루프가 이미 시작된 뒤의 정상 종료에는 영향 없음.

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
        if reason is not None:
            self._stop_reason = reason
        self._control_state = RunnerControlState.STOPPING
        log.info("runner.control_state_changed", state="stopping")
        ws = self._ws
        if ws is not None:
            ws.signal_stop()
        # day-trade: universe 대기 중일 수 있으니 같이 깨움.
        self._universe_ready.set()

    # ---- day-trade 모드 (21단계 Phase B) -----------------------------------

    def enable_shadow(self, tracker: ShadowTracker) -> None:
        """섀도우 세션 활성화 — set_universe() 이전, run() 의 universe 대기 중에 호출.

        dry_run 을 강제해 REST 주문 송신이 구조적으로 불가능하게 만들고, 이후
        잔고 조회(_snapshot_account)가 키움 대신 tracker 가상 원장을 읽게 한다.
        되돌리기 없음 — 섀도우로 켠 세션은 끝까지 섀도우 (중간 전환은 원장 혼선).
        """
        if self._ws is not None:
            raise RuntimeError("enable_shadow() 는 ws.start() 이전에만 호출하세요.")
        self.shadow_tracker = tracker
        self.dry_run = True
        log.info(
            "runner.shadow_enabled",
            initial_cash=tracker.initial_cash,
            log_path=str(tracker.log_path) if tracker.log_path else None,
        )

    def _shadow_account_state(self, previous: AccountState | None) -> AccountState:
        """tracker 가상 원장 → AccountState. 키움 REST 를 전혀 부르지 않는다.

        day-state 디스크 저장/복원도 하지 않음 — 섀도우 한도 소진이 다음 실세션의
        일일 한도(today_notional_used 등)를 오염시키면 안 된다.
        """
        tracker = self.shadow_tracker
        assert tracker is not None  # 호출부가 shadow 분기에서만 부른다
        positions = {sym: qty for sym, qty in tracker.holdings.items() if qty > 0}
        equity = tracker.equity(self._last_closes)
        state = AccountState(
            deposit=tracker.cash,
            total_equity=equity,
            positions=positions,
            avg_prices=dict(tracker.avg_prices),
            day_start_equity=tracker.initial_cash,
            today_notional_used=(previous.today_notional_used if previous is not None else 0.0),
        )
        self._account = state
        return state

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
    ) -> int:
        """보유 모든 종목을 시장가 SELL 송신. dry_run 이면 시뮬 로그만.

        한 종목 실패는 다른 종목으로 진행. 반환값은 *실패한 종목 수*.
        전부 성공(또는 보유 없음)했을 때만 `_liquidation_done` 마킹 —
        실패가 남으면 호출자(_liquidation_loop)가 실패분만 재시도할 수 있게 둔다.
        (성공 종목은 positions 가 0 으로 갱신돼 재호출 시 자연히 제외 = idempotent.)
        """
        # 청산 시작 표시 — 메인 루프의 신규 주문(_apply_signal_gates)·guard SELL
        # (_run_position_guard)이 즉시 멈춘다. 오버나이트 매수/중복 매도 차단.
        self._liquidation_started = True
        if self._liquidation_done:
            log.info("runner.liquidate_skipped", reason="already_done")
            return 0
        # guard 등에서 직전에 발주돼 아직 in-flight 인 SELL 수량은 빼고 청산 —
        # 같은 잔량을 청산 루프가 또 내보내는 중복 매도(800033) 방지.
        self._reap_pending_sells()
        positions: dict[str, int] = {}
        for sym, qty in account.positions.items():
            pending_qty, _ = self._pending_sells.get(sym, (0, None))
            net = qty - max(0, pending_qty)
            if net > 0:
                positions[sym] = net
        if not positions:
            log.info("runner.liquidate_noop", reason="no_positions")
            self._liquidation_done = True
            return 0

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
                # 체결가 근사 = 마지막 봉 종가(없으면 평단가). sell_price 를 로그에
                # 남겨야 리포트 FIFO 집계가 청산분 손익을 실현손익에 포함한다
                # (2026-08-03 섀도우 리포트 과소집계 회귀).
                liq_price = self._last_closes.get(symbol) or buy_price
                log.info(
                    "runner.liquidate_simulated",
                    symbol=symbol,
                    side="sell",
                    qty=qty,
                    reason=reason,
                    buy_price=buy_price,
                    sell_price=liq_price,
                )
                # 시뮬에서도 로컬 포지션 즉시 0 으로 — 다음 청산 호출이 idempotent.
                account.positions[symbol] = 0
                # 섀도우 — 같은 근사가로 가상 청산 체결.
                if self.shadow_tracker is not None and liq_price:
                    self.shadow_tracker.apply(
                        ShadowSignal(
                            symbol=symbol,
                            side=Side.SELL,
                            quantity=qty,
                            price=float(liq_price),
                            ts=datetime.now(KST),
                        )
                    )
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
                # 종료 직전 마지막 잔고 갱신이 키움의 체결 미반영 응답을 받아도
                # holdings 0 으로 보정되게 등록 (run() finally 의 보정 주석 참조).
                self._register_pending_sell(symbol, qty)
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

        # 전부 성공했을 때만 done 마킹 — 실패가 남으면 재시도 여지를 남긴다.
        if failed == 0:
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
        return failed

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

    async def _wallclock_gap_loop(self, detector: WallclockGapDetector) -> None:
        """check_interval_sec 마다 벽시계를 관측해 장중 공백(맥북 슬립)을 사후 감지.

        2026-10-06~16 Amphetamine 꺼짐 — 맥북이 ~17분 주기로만 깨어 수집이 열화됐는데
        heartbeat-check 도 같이 잠들어 경보가 없었다. 러너가 깨어나는 순간 스스로 공백을
        재서 알린다. 공백마다 로그, 텔레그램은 감지기가 하루 1회로 제한.
        알림 실패가 러너를 멈추면 안 되므로 notify 예외는 삼킨다.
        """
        detector.observe()
        while self._control_state is not RunnerControlState.STOPPING:
            try:
                await asyncio.sleep(detector.check_interval_sec)
            except asyncio.CancelledError:
                return
            gap = detector.observe()
            if gap is None:
                continue
            log.warning(
                "runner.wallclock_gap",
                gap_min=round(gap.gap_min, 1),
                since=gap.since.isoformat(),
                until=gap.until.isoformat(),
                notified=gap.notify,
            )
            if not gap.notify:
                continue
            try:
                await self.notifier.notify(
                    "runner.wallclock_gap",
                    gap_min=round(gap.gap_min, 1),
                    since=gap.since.astimezone(KST).strftime("%H:%M"),
                    until=gap.until.astimezone(KST).strftime("%H:%M"),
                )
            except Exception as exc:
                log.warning("runner.wallclock_gap_notify_failed", error=str(exc))

    async def _protection_loop(self, rest: KiwoomRestClient) -> None:
        """잔고 갱신 + kill switch — 메인 tick 루프와 독립된 background task.

        2026-06-10 제3자 검토 — 종전엔 둘 다 tick 루프 본문에만 있어, WS 가 끊겨
        재접속 백오프를 도는 동안(또는 전 종목 VI/거래정지로 체결이 멎으면)
        daily_loss_kill 강제청산까지 같이 멈췄다. REST 는 WS 와 무관하게 살아있으므로
        heartbeat/_liquidation_loop 와 같은 원칙으로 분리. STOPPING 진입 시 즉시 종료.

        잔고 fetch 의 일시 실패는 _snapshot_account 가 previous 를 돌려주며 흡수한다 —
        여기서 예외가 올라오는 건 연속 실패 한도 초과(fail-loud) 뿐이므로, 종전
        메인 루프와 동일하게 러너를 정지시킨다 (알림은 _snapshot_account 가 이미 전송).
        """
        while True:
            try:
                await asyncio.sleep(self.balance_refresh_sec)
            except asyncio.CancelledError:
                return
            if self._control_state is RunnerControlState.STOPPING:
                return
            try:
                account = await self._snapshot_account(rest, previous=self._account)
            except Exception as exc:
                log.error("runner.protection_loop_fatal", error=str(exc), exc_info=exc)
                self.request_stop(reason="balance_failed_fatal")
                return
            try:
                await self._check_kill_switch(rest, account)
            except Exception as exc:
                # kill switch 판정/청산 중 일시 오류 — 다음 주기에 재시도 (보호 지속).
                log.warning("runner.kill_switch_check_failed", error=str(exc))

    async def _liquidate_with_retries(
        self, rest: KiwoomRestClient, account: AccountState, *, reason: str
    ) -> None:
        """liquidate_all + 실패분 유한 재시도. liquidate_at·kill switch 공용.

        청산이 (네트워크 등으로) 일부 실패하면 STOPPING 으로 굳기 전에 실패분만
        유한 횟수 재시도한다. liquidate_all 은 성공 종목 positions 를 0 으로
        만들어 재호출 시 실패분만 다시 송신 = idempotent.
        """
        for attempt in range(self.liquidate_max_retries + 1):
            try:
                failed = await self.liquidate_all(rest, account, reason=reason)
            except Exception as exc:
                log.error("runner.liquidate_loop_error", error=str(exc), exc_info=exc)
                failed = -1  # 예외 = 미확정, 재시도 가치 있음
            if failed == 0:
                return
            if attempt < self.liquidate_max_retries:
                log.warning(
                    "runner.liquidate_retry",
                    failed=failed,
                    attempt=attempt + 1,
                    max_retries=self.liquidate_max_retries,
                    next_delay=self.liquidate_retry_delay_sec,
                )
                await asyncio.sleep(self.liquidate_retry_delay_sec)
            else:
                # 재시도 소진 — 미청산분이 남은 채 종료(다음날 갭 리스크). 명시 경고.
                # 실제로 아직 보유 중인(=청산 못 한) 종목 수를 세어 정확히 알린다.
                remaining = sum(1 for q in account.positions.values() if q > 0)
                log.error("runner.liquidate_giving_up", failed=failed, remaining=remaining)
                await self.notifier.notify("runner.liquidate_giving_up", failed=remaining)

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
                    await self._liquidate_with_retries(rest, account, reason="liquidate_at_reached")
                # 체결 확인용 grace — 너무 짧으면 미체결, 너무 길면 다음날까지 lingering.
                await asyncio.sleep(self.liquidate_grace_sec)
                self.request_stop()
                return
            await asyncio.sleep(1)
