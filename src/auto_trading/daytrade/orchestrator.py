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
from pathlib import Path

import structlog

from ..kiwoom.rest import KiwoomRestClient
from ..market.breadth import Breadth, compute_breadth, evaluate_breadth_gate
from ..market.index import IndexGate, IndexMomentumTracker, index_gate, update_index_momentum
from ..market.rank import Market, RankedSymbol, top_volume
from ..runner import PaperRunner
from ..strategy.base import Strategy
from ..strategy.intraday import IntradayMomentum
from .profile import DaytradeBreadthGate

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


async def _read_settled_breadth(
    snapshot_dir: Path,
    *,
    min_sample: int,
    timeout_sec: int,
    poll_sec: float = 5.0,
) -> Breadth:
    """스냅샷 라운드가 다 채워질 때까지 기다렸다 breadth 측정.

    무엇/왜: 스냅샷 잡은 개장 라운드를 시총 1→200위 순서로 한 줄씩 append 하므로,
    작성 도중 읽으면 시총 상위만 표본에 잡혀 시장 폭이 대형주로 편향된다.
    표본 수가 **두 번 연속 같으면**(=작성 완료) 채택 — 그날 유니버스가 200 미만이어도
    자동 대응. `timeout_sec` 안에 `min_sample` 도 못 채우면 마지막 측정을 그대로 반환
    (대개 unavailable → 호출자 fail-open).

    market/ 규칙(읽기 전용·sleep 없음)을 지키려 대기 루프는 여기(daytrade)에 둔다.
    """
    breadth = compute_breadth(snapshot_dir, min_sample=min_sample)
    if not breadth.unavailable or timeout_sec <= 0:
        return breadth

    # 안정 여부는 min_sample 컷오프 없이 원시 줄 수로 판단해야 정확하다.
    prev_sample = compute_breadth(snapshot_dir).sample_size
    waited = 0.0
    while waited < timeout_sec:
        await asyncio.sleep(poll_sec)
        waited += poll_sec
        raw = compute_breadth(snapshot_dir)
        if raw.sample_size >= min_sample and raw.sample_size == prev_sample:
            log.info("market.breadth.settled", sample=raw.sample_size, waited=waited)
            return raw
        prev_sample = raw.sample_size

    log.warning(
        "market.breadth.settle_timeout",
        sample=prev_sample,
        min_sample=min_sample,
        waited=waited,
    )
    return compute_breadth(snapshot_dir, min_sample=min_sample)


async def discover_and_arm(
    rest: KiwoomRestClient,
    runner: PaperRunner,
    *,
    top_n: int = 3,
    strategy_factory: Callable[..., Strategy] | None = None,
    markets: tuple[Market, ...] = ("KOSPI", "KOSDAQ"),
    exclude_etf: bool = True,
    breadth_gate_config: DaytradeBreadthGate | None = None,
    avoid_symbols: tuple[str, ...] | list[str] = (),
    untradeable_symbols: tuple[str, ...] | list[str] = (),
) -> DiscoveryResult:
    """발굴 → 게이트 → set_universe 한 번에. 통과 종목 0건이면 set_universe 호출 X.

    무엇/왜:
    - 한 트랜잭션으로 묶어야 부분 실패(예: 게이트 한 시장 fetch 실패) 가 명확히 드러남.
    - strategy_factory 가 None 이면 IntradayMomentum(weight=1/top_n) 균등 분할.
    - exclude_etf=True (기본) 면 거래량 상위에 끼는 인버스 ETF/ETN/SPAC 제외 — 일반
      주식 day-trade 의도와 일치. False 면 키움 거래량 순위 그대로.
    - **발굴 보강**: 후보를 top_n 보다 넉넉히(pool) 받아, 거래불가 이력
      (`untradeable_symbols`)·회피(`avoid_symbols`)·지수 게이트 차단을 건너뛰고
      *다음 순위* 로 top_n 을 채운다. 거래불가 종목이 슬롯을 통째로 낭비하던
      문제(2026-06-16 004870 매매제한)를 사전에 차단.

    어떻게 확인: 단위 테스트가 fake top_volume/index_gate 결과로 chosen/blocked 분기 검증.
    """
    # 거래불가 이력은 후보 풀에서 통째로 제외(슬롯 낭비 방지). avoid 는 아래에서
    # blocked 로 명시 라벨링하려고 풀에는 남겨 둔다.
    pool_n = max(top_n * 3, top_n + 7)
    candidates = await top_volume(
        rest,
        top_n=pool_n,
        markets=markets,
        exclude_etf=exclude_etf,
        exclude_symbols=tuple(untradeable_symbols),
    )
    if not candidates:
        msg = "거래량 상위 후보를 가져오지 못했습니다 — universe 비움."
        log.warning("daytrade.discovery_empty")
        return DiscoveryResult(chosen=[], blocked=[], gates={}, note_ko=msg)

    # 후보가 속한 시장만 게이트 조회 — 둘 다 안 쓰면 한 번만.
    needed_markets = sorted({c.market for c in candidates})
    gates: dict[str, IndexGate] = {}
    for m in needed_markets:
        gates[m] = await index_gate(rest, m)

    # Phase 1 — 시총 200 breadth 게이트. enabled=False (기본) 면 skip → 회귀 0.
    if breadth_gate_config is not None and breadth_gate_config.enabled:
        breadth = await _read_settled_breadth(
            Path(breadth_gate_config.snapshot_dir),
            min_sample=breadth_gate_config.min_sample,
            timeout_sec=breadth_gate_config.settle_timeout_sec,
        )
        result = evaluate_breadth_gate(
            breadth,
            min_advancing_pct=breadth_gate_config.min_advancing_pct,
            min_mean_change_pct=breadth_gate_config.min_mean_change_pct,
        )
        log.info(
            "daytrade.breadth_gate",
            passed=result.passed,
            reason=result.reason,
            advancing_pct=round(breadth.advancing_pct, 3),
            mean_change_pct=breadth.mean_change_pct,
            sample=breadth.sample_size,
        )
        if not result.passed:
            # 모든 후보 차단 — 명시적 reason 으로 blocked 마킹.
            note = (
                f"시총 200 breadth 게이트 차단 ({result.reason}: "
                f"adv={breadth.advancing_pct:.0%}, mean={breadth.mean_change_pct:+.2f}%) "
                f"— 오늘은 거래 없이 종료합니다."
            )
            blocked_all = [(c.symbol, f"breadth_{result.reason}") for c in candidates]
            names_all = {c.symbol: c.name for c in candidates if c.name}
            # 일일 리포트는 daytrade.discovery 이벤트를 파싱해 만들어진다 — 이 이벤트
            # 없이 조기 리턴하면 breadth 차단일 리포트가 통째로 스킵됨(2026-07-07·08 공백).
            log.info(
                "daytrade.discovery",
                candidates=[(c.symbol, c.market, c.volume) for c in candidates],
                chosen=[],
                blocked=blocked_all,
                gates={m: g.above_5_and_20 for m, g in gates.items()},
                names=names_all,
            )
            log.info("daytrade.universe_chosen", symbols=[], reason="breadth_blocked")
            return DiscoveryResult(
                chosen=[], blocked=blocked_all, gates=gates, note_ko=note, names=names_all
            )

    chosen: list[RankedSymbol] = []
    blocked: list[tuple[str, str]] = []
    avoid_set = {s for s in avoid_symbols if s}
    for cand in candidates:
        if len(chosen) >= top_n:
            break  # top_n 충족 — 나머지 풀은 미사용(차단 아님).
        if cand.symbol in avoid_set:
            blocked.append((cand.symbol, "avoid_symbols"))
            continue
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
        note = f"후보 {len(candidates)}건 모두 지수 게이트 차단 — 오늘은 거래 없이 종료합니다."
        log.info("daytrade.universe_chosen", symbols=[], reason="all_blocked")
        return DiscoveryResult(chosen=[], blocked=blocked, gates=gates, note_ko=note, names=names)

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
    return DiscoveryResult(chosen=chosen, blocked=blocked, gates=gates, note_ko=note, names=names)


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
