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

from collections.abc import Callable
from dataclasses import dataclass

import structlog

from ..kiwoom.rest import KiwoomRestClient
from ..market.index import IndexGate, index_gate
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


def _default_strategy_factory(weight: float) -> Callable[[str], Strategy]:
    """N 분할 균등 매수용 IntradayMomentum 팩토리."""

    def _factory(_symbol: str) -> Strategy:
        return IntradayMomentum(weight=weight)

    return _factory


async def discover_and_arm(
    rest: KiwoomRestClient,
    runner: PaperRunner,
    *,
    top_n: int = 3,
    strategy_factory: Callable[[str], Strategy] | None = None,
    markets: tuple[Market, ...] = ("KOSPI", "KOSDAQ"),
) -> DiscoveryResult:
    """발굴 → 게이트 → set_universe 한 번에. 통과 종목 0건이면 set_universe 호출 X.

    무엇/왜:
    - 한 트랜잭션으로 묶어야 부분 실패(예: 게이트 한 시장 fetch 실패) 가 명확히 드러남.
    - strategy_factory 가 None 이면 IntradayMomentum(weight=1/top_n) 균등 분할.

    어떻게 확인: 단위 테스트가 fake top_volume/index_gate 결과로 chosen/blocked 분기 검증.
    """
    candidates = await top_volume(rest, top_n=top_n, markets=markets)
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

    log.info(
        "daytrade.discovery",
        candidates=[(c.symbol, c.market, c.volume) for c in candidates],
        chosen=[c.symbol for c in chosen],
        blocked=blocked,
        gates={m: g.above_5_and_20 for m, g in gates.items()},
    )

    if not chosen:
        note = f"후보 {len(candidates)}건 모두 지수 게이트 차단 — 매수 없이 청산 시각까지 대기."
        log.info("daytrade.universe_chosen", symbols=[], reason="all_blocked")
        return DiscoveryResult(chosen=[], blocked=blocked, gates=gates, note_ko=note)

    factory = strategy_factory or _default_strategy_factory(weight=1.0 / max(1, top_n))
    symbols = [c.symbol for c in chosen]
    strategies = {sym: factory(sym) for sym in symbols}
    runner.set_universe(symbols, strategies)
    log.info(
        "daytrade.universe_chosen",
        symbols=symbols,
        markets={c.symbol: c.market for c in chosen},
    )

    note = f"매수 후보 {len(chosen)}/{len(candidates)}: {', '.join(symbols)}"
    if blocked:
        note += f" (차단 {len(blocked)})"
    return DiscoveryResult(chosen=chosen, blocked=blocked, gates=gates, note_ko=note)


__all__ = ["DiscoveryResult", "discover_and_arm"]
