"""day-trade 발굴·지수 게이트·universe 주입을 묶은 오케스트레이터.

흐름:
1. `top_volume()` 으로 KOSPI+KOSDAQ 거래량 상위 N 후보.
2. 각 종목의 소속 시장 지수(KOSPI 또는 KOSDAQ) 5/20 SMA 게이트 통과 여부 확인.
   같은 시장은 한 번만 조회해 캐싱.
3. 통과 종목만 IntradayMomentum 인스턴스(weight=1/N) 로 묶어 `runner.set_universe()`.
4. 결과(`DiscoveryResult`)를 호출자에게 돌려 로그·텔레그램 푸시.

이 모듈은 어떤 타이밍 결정도 하지 않음 — 호출 시점은 CLI(`paper-day`) 가 책임.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

import structlog

from ..kiwoom.rest import KiwoomRestClient
from ..market.index import IndexGate, IndexMomentumTracker, index_gate, update_index_momentum
from ..market.rank import Market, RankedSymbol, top_volume
from ..runner import PaperRunner
from ..strategy.base import Strategy
from ..strategy.intraday import IntradayMomentum

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class DiscoveryResult:
    """발굴·게이트 통과 결과 요약. CLI 출력·텔레그램 푸시·테스트 검증용."""

    chosen: list[RankedSymbol]  # set_universe 까지 들어간 종목들
    blocked: list[tuple[str, str]]  # (symbol, reason) — 게이트 차단 / 후보 부족
    gates: dict[str, IndexGate]  # 시장→게이트 (디버그·로그용)
    note_ko: str  # 사람이 읽는 한 줄 요약
    # 코드→종목명. chosen 뿐 아니라 blocked 후보까지 포함한 전체 후보 매핑 —
    # 텔레그램 발굴 알림이 차단 종목도 "005930 삼성전자" 로 보여줄 수 있게.
    names: dict[str, str] = field(default_factory=dict)


def _default_strategy_factory(weight: float) -> Callable[..., Strategy]:
    """N 분할 균등 매수용 IntradayMomentum 팩토리.

    market 키워드는 후방 호환을 위해 받기만 하고 사용하지 않는다 — 프로파일 기반
    팩토리(`cli_helpers.build_profile_factory`) 가 시장별 모멘텀 게이트를 활용한다.
    """

    def _factory(_symbol: str, *, market: Market | None = None) -> Strategy:
        del market  # 미사용
        return IntradayMomentum(weight=weight)

    return _factory


async def discover_and_arm(
    rest: KiwoomRestClient,
    runner: PaperRunner,
    *,
    top_n: int = 3,
    strategy_factory: Callable[..., Strategy] | None = None,
    markets: tuple[Market, ...] = ("KOSPI", "KOSDAQ"),
    exclude_etf: bool = True,
) -> DiscoveryResult:
    """발굴 → 게이트 → set_universe 한 번에. 통과 종목 0건이면 set_universe 호출 X.

    무엇/왜:
    - 한 트랜잭션으로 묶어야 부분 실패(예: 게이트 한 시장 fetch 실패) 가 명확히 드러남.
    - strategy_factory 가 None 이면 IntradayMomentum(weight=1/top_n) 균등 분할.
    - exclude_etf=True (기본) 면 거래량 상위에 끼는 인버스 ETF/ETN/SPAC 제외 — 일반
      주식 day-trade 의도와 일치. False 면 키움 거래량 순위 그대로.

    어떻게 확인: 단위 테스트가 fake top_volume/index_gate 결과로 chosen/blocked 분기 검증.
    """
    candidates = await top_volume(rest, top_n=top_n, markets=markets, exclude_etf=exclude_etf)
    if not candidates:
        msg = "거래량 상위 후보를 가져오지 못했습니다 — universe 비움."
        log.warning("daytrade.discovery_empty")
        return DiscoveryResult(chosen=[], blocked=[], gates={}, note_ko=msg)

    # 후보가 속한 시장만 게이트 조회 — 둘 다 안 쓰면 한 번만.
    needed_markets = sorted({c.market for c in candidates})
    gates: dict[str, IndexGate] = {}
    for m in needed_markets:
        gates[m] = await index_gate(rest, m)

    chosen: list[RankedSymbol] = []
    blocked: list[tuple[str, str]] = []
    for cand in candidates:
        gate = gates.get(cand.market)
        if gate is None or not gate.above_5_and_20:
            reason = f"{cand.market}_below_sma" if gate else f"{cand.market}_gate_unavailable"
            blocked.append((cand.symbol, reason))
            continue
        chosen.append(cand)

    # names: 일일 리포트가 코드 옆에 종목명을 붙일 수 있도록 후보·채택 종목의
    # 코드→이름 매핑을 함께 남긴다. RankedSymbol.name 은 키움 응답에서 파싱됨.
    names = {c.symbol: c.name for c in candidates if c.name}
    log.info(
        "daytrade.discovery",
        candidates=[(c.symbol, c.market, c.volume) for c in candidates],
        chosen=[c.symbol for c in chosen],
        blocked=blocked,
        gates={m: g.above_5_and_20 for m, g in gates.items()},
        names=names,
    )

    if not chosen:
        note = f"후보 {len(candidates)}건 모두 지수 게이트 차단 — 매수 없이 청산 시각까지 대기."
        log.info("daytrade.universe_chosen", symbols=[], reason="all_blocked")
        return DiscoveryResult(
            chosen=[], blocked=blocked, gates=gates, note_ko=note, names=names
        )

    factory = strategy_factory or _default_strategy_factory(weight=1.0 / max(1, top_n))
    symbols = [c.symbol for c in chosen]
    strategies = {c.symbol: factory(c.symbol, market=c.market) for c in chosen}
    # 코드+종목명을 텔레그램 메시지에서 같이 보이려면 set_universe 가 이름까지 받아야 함.
    chosen_names = {c.symbol: c.name for c in chosen if c.name}
    runner.set_universe(symbols, strategies, names=chosen_names)
    log.info(
        "daytrade.universe_chosen",
        symbols=symbols,
        markets={c.symbol: c.market for c in chosen},
        names={c.symbol: c.name for c in chosen if c.name},
    )

    note = f"매수 후보 {len(chosen)}/{len(candidates)}: {', '.join(symbols)}"
    if blocked:
        note += f" (차단 {len(blocked)})"
    return DiscoveryResult(
        chosen=chosen, blocked=blocked, gates=gates, note_ko=note, names=names
    )


async def _momentum_loop(
    rest: KiwoomRestClient,
    tracker: IndexMomentumTracker,
    markets: tuple[Market, ...],
    interval_sec: int,
    lookback_min: int,
) -> None:
    """1분 주기로 KOSPI/KOSDAQ 지수 가격 fetch + tracker.push.

    무엇/왜:
    - tracker 는 인메모리 시계열 — 별도 task 로 갱신 안 하면 momentum() 결과가 안 변함.
    - paper-day 한 사이클에 한 번 시동, runner stop 시 호출자가 task.cancel() 책임.

    어떻게 확인: tests/test_daytrade_orchestrator.py 의 momentum_loop 단위 테스트.
    """
    while True:
        for market in markets:
            try:
                await update_index_momentum(rest, tracker, market, lookback_min=lookback_min)
            except Exception:
                log.warning("daytrade.momentum_update_failed", market=market, exc_info=True)
        await asyncio.sleep(interval_sec)


def start_momentum_loop(
    rest: KiwoomRestClient,
    tracker: IndexMomentumTracker,
    *,
    markets: tuple[Market, ...] = ("KOSPI", "KOSDAQ"),
    interval_sec: int = 60,
    lookback_min: int = 5,
) -> asyncio.Task[None]:
    """`_momentum_loop` 을 background task 로 시동. 호출자가 cancel·await 책임.

    무엇/왜: paper-day 1 사이클에 1개 task. runner.run() 종료 후 cli_helpers 가 cancel.
    """
    return asyncio.create_task(
        _momentum_loop(rest, tracker, markets, interval_sec, lookback_min),
        name="index-momentum-loop",
    )


__all__ = ["DiscoveryResult", "discover_and_arm", "start_momentum_loop"]
