"""DailyReport 도메인 dataclass — 순수 데이터(로직·외부 의존 없음).

빌더(`builder.py`)·집계(`aggregate.py`)·I/O(`persist.py`) 가 모두 이 모델을 공유한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TradeRecord:
    """매수·매도 1건. price 는 로그에 남아있을 때만(시장가는 None)."""

    symbol: str
    side: str  # "buy" / "sell"
    quantity: int
    price: float | None
    simulated: bool  # dry-run 시뮬이면 True
    name: str | None = None  # 종목명 — 발굴 로그에서 알 수 있을 때만 채움
    # 데이터 기반 분석용 — 체결 시각(KST "HH:MM", 로그 timestamp 변환)과 사유.
    # 매수 reason = 전략 Signal.reason, 매도 reason = guard 사유(손절/트레일링/부분익절)
    # 또는 전략 매도 사유. 로그에 없으면 None.
    time_kst: str | None = None
    reason: str | None = None
    # 진입 시점 피처(매수 레코드만) — topcap 스냅샷 조인. 스냅샷 10분 캐던스라 근사.
    change_pct_at_entry: float | None = None  # 진입 무렵 종목 등락률(%)
    rank_at_entry: int | None = None  # 진입 무렵 시총 순위


@dataclass(frozen=True)
class LiquidationRecord:
    """청산 1건. 매수 가격까지 알면 buy_price 채움 → pnl 계산."""

    symbol: str
    quantity: int
    sell_price: float | None
    buy_price: float | None
    simulated: bool
    name: str | None = None
    # EOD 청산 시각(KST "HH:MM")과 사유("liquidate_at_reached"/"daily_loss_kill" 등).
    time_kst: str | None = None
    reason: str | None = None

    @property
    def pnl(self) -> float | None:
        if self.sell_price is None or self.buy_price is None:
            return None
        return (self.sell_price - self.buy_price) * self.quantity

    @property
    def pnl_pct(self) -> float | None:
        if self.sell_price is None or self.buy_price is None or self.buy_price == 0:
            return None
        return (self.sell_price / self.buy_price - 1.0) * 100.0


@dataclass(frozen=True)
class RoundTrip:
    """종목 왕복(매수→매도/청산) 1건 — 데이터 기반 전략 분석의 기본 단위.

    FIFO 매칭으로 매수 lot 과 매도/청산을 짝지어 만든다. 매수가·매도가가 둘 다
    있는 분량만 손익이 채워진다(가격 결손 분은 pnl=None). 승률·손익비·보유시간·
    사유별 성과 분석에 그대로 쓰인다.
    """

    symbol: str
    name: str | None
    quantity: int
    buy_price: float | None
    sell_price: float | None
    buy_time_kst: str | None
    sell_time_kst: str | None
    holding_minutes: int | None
    pnl: float | None
    pnl_pct: float | None
    exit_reason: str | None  # guard 사유 / "eod" 계열 / 전략 매도 사유
    # 보유구간 미실현 최대이익/최대손실(%) — topcap 스냅샷 조인. 매수가 기준.
    # "익절을 너무 빨리/늦게 했나" 진단용. 스냅샷 10분 캐던스라 거친 추정.
    mfe_pct: float | None = None
    mae_pct: float | None = None


@dataclass(frozen=True)
class BlockedOutcome:
    """발굴에서 차단된 종목의 그날 실제 움직임 — 게이트 기회비용(반사실) 분석용.

    "차단 안 했으면 진입했을 텐데" 의 사후 검증: 진입했다면 일중(시초→종가)·
    일간(전일대비) 수익률이 얼마였는지. topcap 스냅샷에 그 종목이 없으면 None.
    """

    symbol: str
    name: str | None
    reason: str  # 차단 사유 (breadth_* / *_below_sma / avoid_symbols 등)
    intraday_pct: float | None  # 그날 시초→종가 변화율(%)
    day_change_pct: float | None  # 종가 기준 전일대비 등락률(%)


@dataclass(frozen=True)
class MarketGateInfo:
    """시장 게이트 1건 (KOSPI / KOSDAQ)."""

    market: str
    last: float | None
    sma5: float | None
    sma20: float | None
    above: bool


@dataclass(frozen=True)
class TimelineEvent:
    """일일 리포트의 시계열 한 줄.

    2026-05-11 회귀 후속 — 사용자가 "오늘 09:01 무슨 일이 일어났지?" 를 즉시 파악할 수
    있게 발굴·매수·부분 익절·kill switch·청산 시작을 시각 순으로 한 줄씩 합쳐서 보여줌.
    text 는 이미 사람이 읽는 형식으로 빌더가 채움 — 포매터는 시각 + text 만 출력.
    """

    time_kst: str  # "HH:MM" KST. 로그가 UTC ISO 8601 라 빌더가 변환.
    # "discovery" | "buy" | "sell" | "partial_take" | "stop_loss" | "trailing"
    # | "kill_switch" | "liquidate"
    kind: str
    text: str  # 한국어 한 줄 — 종목·수량·가격·비율 등 포함


@dataclass(frozen=True)
class ClosingHolding:
    """마감 시점 보유 종목 1건. 러너 종료 직후 키움 잔고 스냅샷에서 추출."""

    symbol: str
    name: str
    quantity: int
    avg_price: float
    current_price: float
    market_value: float
    pnl: float
    pnl_pct: float


@dataclass(frozen=True)
class DailyReport:
    """하루치 트레이딩 요약. JSON 직렬화 → `data/reports/daily-YYYY-MM-DD.json`."""

    date: str  # YYYY-MM-DD
    mode: str  # "day-trade" / "swing"
    dry_run: bool
    discovery_chosen: list[str]
    discovery_blocked: list[tuple[str, str]]
    market_gates: list[MarketGateInfo]
    buys: list[TradeRecord]
    liquidations: list[LiquidationRecord]
    error_count: int
    note_ko: str
    # 메타. 누적 손익(가능한 경우만 채워진다 — 시장가 매수/매도 가격이 둘 다 있을 때).
    realized_pnl: float | None = None
    realized_pnl_pct: float | None = None
    # 장중 청산(stop-loss / trailing / partial-take) SELL 체결. EOD 강제청산은
    # liquidations 가 따로 담는다 — 두 경로의 이벤트명이 달라 중복 집계되지 않는다.
    # 2026-05-16: 이전엔 side=="buy" 만 추출하고 SELL 을 통째로 버려 realized_pnl 이
    # 항상 None 이었음(주간 인사이트가 "성과 검증 불가" 로 오판). 이 필드로 복구.
    sells: list[TradeRecord] = field(default_factory=list)
    excluded_etf_count: int = 0
    rank_candidates: list[str] = field(default_factory=list)
    # 코드→종목명. discovery 로그의 names 필드에서 흡수. 없는 종목은 키 자체가 없음.
    discovery_chosen_names: dict[str, str] = field(default_factory=dict)
    # 마감 시점 잔고 — 러너 인메모리 snapshot 가용 시만 채움 (자동 호출 시 ✓, 수동 재생성 X).
    # day-trade 청산 후엔 closing_holdings 가 빈 리스트인 게 정상.
    closing_cash: int | None = None
    closing_equity: int | None = None
    closing_total_pnl: int | None = None
    closing_holdings: list[ClosingHolding] = field(default_factory=list)
    # 23단계 Phase A · 비용 추정 — mock/시뮬은 키움이 비용 0 으로 응답하므로 daily.py 에서 계산해 주입.
    # 실거래는 키움 응답에 비용이 들어있지만, 일관성·교차 검증 차원에서 동일 추정도 함께 저장.
    total_fees: float | None = None  # 매수+매도 수수료 합 (COST_COMMISSION_RATE * 2)
    total_taxes: float | None = None  # 매도 거래세 합 (COST_TAX_RATE_SELL)
    total_slippage: float | None = None  # 매수+매도 슬리피지 합 (COST_SLIPPAGE_BPS * 2)
    realized_pnl_after_cost: float | None = None  # realized_pnl - (fees + taxes + slippage)
    # 2026-05-11 회귀 후속 — 시계열 한 줄 요약. 로그에 timestamp 가 없거나 파싱 불가면 빈 리스트.
    timeline: list[TimelineEvent] = field(default_factory=list)
    # 과매매 가시화 — 손실 핵심 원인이 장중 과회전이라 사용자가 눈으로 보게 한다.
    # traded_notional: 전 체결 거래대금 합(가격 없는 체결은 제외, 전부 없으면 None).
    # turnover_ratio: 거래대금 ÷ 당일 시작자본. snapshot.account.day_start_equity 가
    #   있을 때만 — 분모 추정 금지(없으면 None).
    traded_notional: float | None = None
    turnover_ratio: float | None = None
    # error_count 분해 — "에러 N건" 통짜는 진단 불가. 0 인 항목은 표시단에서 생략.
    orders_failed_count: int = 0
    liquidations_failed_count: int = 0
    balance_failure_count: int = 0
    fatal_count: int = 0
    # === 데이터 기반 전략 수립용 맥락(2026-06-15) ===
    # 그날 시장 국면(daytrade.regime 로그) — regime_router 활성일 때만, 아니면 None.
    regime: str | None = None
    # 시총200 breadth 게이트 측정치(daytrade.breadth_gate 로그). 게이트 비활성이면 None.
    breadth_advancing_pct: float | None = None
    breadth_mean_change_pct: float | None = None
    breadth_passed: bool | None = None
    # 종목 왕복(매수↔매도/청산) 단위 손익 — 승률·보유시간·사유별 성과 분석의 기본 재료.
    round_trips: list[RoundTrip] = field(default_factory=list)
    # 차단 종목의 그날 움직임 — 게이트 기회비용(반사실) 분석. 스냅샷 없으면 빈 리스트.
    blocked_outcomes: list[BlockedOutcome] = field(default_factory=list)
    # 매수 0건인 날의 사유(초보 친화 한국어 1줄). 매수가 있으면 None.
    no_trade_reason: str | None = None
    # 2026-06-19 — "장 초반 쏠림" 가시화. 개장 웨이브가 일일 매수대금 한도를 소진하면
    # 오후 재진입 신호가 매수여력 부족(budget too small/quantity 0)으로 전부 막힌다.
    # today_notional_used: 그날 누적 매수대금(snapshot 가용 시만 — 수동 재생성은 None).
    # reentry_blocked_count: 매수여력 부족으로 거부된 재진입 신호 수(로그만으로 산출).
    today_notional_used: int | None = None
    reentry_blocked_count: int = 0
