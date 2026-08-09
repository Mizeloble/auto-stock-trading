"""apply_daytrade_profile_overrides 단위 테스트.

PaperDayOverrides 가 CLI 인자 vs 프로파일 우선순위를 정확히 결정하는지 격리 검증.
build_paper_day_runner / paper_day_main 등은 mocking 부담이 커서 통합 테스트
(test_cli_paper_day.py) 로 검증. 단, PR27 (5/12 회귀) 의 heartbeat 주입 한 줄은
runner 필드만 확인하면 되므로 여기서 같이 커버.
"""

from __future__ import annotations

from datetime import date, time
from pathlib import Path

import pytest

from auto_trading.daytrade.cli_helpers import (
    PaperDayOverrides,
    apply_daytrade_profile_overrides,
    build_paper_day_runner,
    merge_base_avoid_symbols,
    record_alt_ranking,
)
from auto_trading.daytrade.orchestrator import DiscoveryResult
from auto_trading.daytrade.profile import (
    DaytradeProfile,
    DaytradeRiskLimits,
    DaytradeStrategyParams,
)
from auto_trading.heartbeat import Heartbeat
from auto_trading.market.rank import RankedSymbol


def _write_profile(
    path: Path,
    *,
    strategy: str = "intraday_momentum",
    top_n: int = 5,
    liquidate: str = "14:50",
    discover_delay_sec: int = 60,
    exclude_etf: bool = True,
) -> None:
    profile = DaytradeProfile(
        name="test",
        strategy=strategy,  # type: ignore[arg-type]
        top_n=top_n,
        liquidate=liquidate,
        discover_delay_sec=discover_delay_sec,
        exclude_etf=exclude_etf,
        params=DaytradeStrategyParams(),
        risk=DaytradeRiskLimits(),
    )
    profile.dump_yaml(path)


def test_no_profile_path_returns_cli_args_passthrough() -> None:
    """profile_path=None → CLI 인자 그대로, summary_msg=None."""
    result = apply_daytrade_profile_overrides(
        None,
        top_n=3,
        liquidate="15:15",
        discover_delay_sec=30,
        include_etf=False,
    )
    assert result == PaperDayOverrides(
        profile=None,
        top_n=3,
        liquidate="15:15",
        discover_delay_sec=30,
        include_etf=False,
        summary_msg=None,
    )


def test_profile_overrides_top_n_liquidate_discover_etf(tmp_path: Path) -> None:
    """프로파일 지정 시 4개 필드(top_n/liquidate/discover_delay_sec/exclude_etf) 가 덮어쓰기."""
    profile_yaml = tmp_path / "p.yaml"
    _write_profile(
        profile_yaml,
        top_n=7,
        liquidate="14:50",
        discover_delay_sec=120,
        exclude_etf=False,  # ETF 포함 (기본과 반대)
    )

    result = apply_daytrade_profile_overrides(
        profile_yaml,
        top_n=3,  # CLI 인자는 무시되어야
        liquidate="15:15",
        discover_delay_sec=30,
        include_etf=False,
    )

    assert result.profile is not None
    assert result.top_n == 7
    assert result.liquidate == "14:50"
    assert result.discover_delay_sec == 120
    assert result.include_etf is True  # exclude_etf=False → include_etf=True


def test_profile_summary_msg_includes_strategy_top_n_liquidate(tmp_path: Path) -> None:
    profile_yaml = tmp_path / "p.yaml"
    _write_profile(
        profile_yaml,
        strategy="index_momentum_daytrade",
        top_n=5,
        liquidate="14:55",
    )

    result = apply_daytrade_profile_overrides(
        profile_yaml,
        top_n=3,
        liquidate="15:15",
        discover_delay_sec=30,
        include_etf=False,
    )

    assert result.summary_msg is not None
    assert "p.yaml" in result.summary_msg
    assert "index_momentum_daytrade" in result.summary_msg
    assert "top_n=5" in result.summary_msg
    assert "14:55" in result.summary_msg


def test_profile_exclude_etf_true_yields_include_etf_false(tmp_path: Path) -> None:
    """exclude_etf=True (보수적 기본) → include_etf=False."""
    profile_yaml = tmp_path / "p.yaml"
    _write_profile(profile_yaml, exclude_etf=True)

    result = apply_daytrade_profile_overrides(
        profile_yaml,
        top_n=3,
        liquidate="15:15",
        discover_delay_sec=30,
        include_etf=True,  # CLI 의 include_etf 값은 무시
    )

    assert result.include_etf is False


def test_invalid_profile_yaml_raises(tmp_path: Path) -> None:
    """깨진 YAML(필수 필드 누락) 은 pydantic ValidationError 로 그대로 전파."""
    from pydantic import ValidationError

    bad = tmp_path / "bad.yaml"
    bad.write_text("strategy: not_a_real_strategy\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        apply_daytrade_profile_overrides(
            bad,
            top_n=3,
            liquidate="15:15",
            discover_delay_sec=30,
            include_etf=False,
        )


def _profile(name: str, avoid: list[str]) -> DaytradeProfile:
    return DaytradeProfile(
        name=name,
        strategy="index_momentum_daytrade",
        params=DaytradeStrategyParams(),
        risk=DaytradeRiskLimits(),
        avoid_symbols=avoid,
    )


def test_merge_base_avoid_symbols_adds_missing_and_keeps_order() -> None:
    """베이스(current)에만 있는 회피 종목이 라우팅된 프로파일에 합쳐진다.

    드리프트 시나리오: 주간 리뷰가 current 에만 회피를 추가 → 라우팅된 날 미적용.
    기존 목록은 순서 보존, 새 항목은 정렬돼 뒤에 붙는다.
    """
    routed = _profile("highvol", avoid=["356680"])
    base = _profile("current", avoid=["004870", "001470", "356680"])

    added = merge_base_avoid_symbols(routed, base=base)

    assert added == ["001470", "004870"]
    assert routed.avoid_symbols == ["356680", "001470", "004870"]


def test_merge_base_avoid_symbols_noop_when_already_covered() -> None:
    """베이스 목록이 이미 전부 있으면 변경 없음 + 빈 리스트 반환."""
    routed = _profile("highvol", avoid=["001470", "356680"])
    base = _profile("current", avoid=["356680"])

    added = merge_base_avoid_symbols(routed, base=base)

    assert added == []
    assert routed.avoid_symbols == ["001470", "356680"]


def test_merge_base_avoid_symbols_ignores_empty_entries() -> None:
    """베이스에 빈 문자열이 섞여 있어도 회피 목록에 유입되지 않는다."""
    routed = _profile("highvol", avoid=[])
    base = _profile("current", avoid=["", "005930"])

    added = merge_base_avoid_symbols(routed, base=base)

    assert added == ["005930"]
    assert routed.avoid_symbols == ["005930"]


def _ranked(
    symbol: str,
    *,
    volume: int = 100,
    price: float = 1000.0,
    metric: float | None = None,
    rank: int | None = None,
) -> RankedSymbol:
    return RankedSymbol(
        symbol=symbol,
        market="KOSPI",
        name=f"이름{symbol}",
        volume=volume,
        price=price,
        metric=metric,
        rank=rank,
    )


def _patch_alt_pools(monkeypatch, *, value=None, foreign=None, institution=None) -> None:
    """record_alt_ranking 이 부르는 순위 조회 3종을 한 번에 페이크로 교체.

    각 인자는 `(rest, top_n, exclude_etf) -> list[RankedSymbol]` 을 흉내 내는 async
    콜러블이거나, 그대로 돌려줄 리스트. None 이면 빈 풀(= 그 축 스킵).
    """
    import auto_trading.daytrade.cli_helpers as ch

    def _as_fetch(spec):
        if callable(spec):
            return spec

        async def _fixed(_rest, **_kw):
            return list(spec or [])

        return _fixed

    fake_value = _as_fetch(value)
    fake_foreign = _as_fetch(foreign)
    fake_institution = _as_fetch(institution)

    async def fake_top_net_buy(rest, *, investor, top_n, exclude_etf):
        fetch = fake_foreign if investor == "foreign" else fake_institution
        return await fetch(rest, top_n=top_n, exclude_etf=exclude_etf)

    monkeypatch.setattr(ch, "top_value", fake_value)
    monkeypatch.setattr(ch, "top_net_buy", fake_top_net_buy)


def _discovery(chosen: list[str], blocked: list[str]) -> DiscoveryResult:
    return DiscoveryResult(
        chosen=[_ranked(s) for s in chosen],
        blocked=[(s, "reason") for s in blocked],
        gates={},
        note_ko="테스트",
    )


async def test_record_alt_ranking_appends_jsonl(tmp_path: Path, monkeypatch) -> None:
    """발굴 결과 + 대안 순위 풀 3종이 JSONL 한 줄(v2)로 축적된다."""
    import json

    async def fake_top_value(_rest, **kw):
        assert kw["top_n"] == 10 and kw["exclude_etf"] is True
        return [_ranked("222222", volume=10, price=300_000.0), _ranked("111111")]

    _patch_alt_pools(
        monkeypatch,
        value=fake_top_value,
        foreign=[_ranked("333333", volume=0, price=0.0, metric=594_000.0, rank=1)],
        institution=[_ranked("444444", volume=0, price=0.0, metric=-1_000.0, rank=2)],
    )
    out = tmp_path / "alt-rank.jsonl"

    path = await record_alt_ranking(
        None,  # type: ignore[arg-type]  # 순위 조회를 전부 패치했으므로 rest 미사용
        _discovery(chosen=["005930"], blocked=["356680"]),
        pool_n=10,
        exclude_etf=True,
        out_path=out,
    )

    assert path == out
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["v"] == 2
    assert rec["volume_chosen"] == ["005930"]
    assert rec["volume_pool"] == ["005930", "356680"]
    pools = rec["alt_pools"]
    assert set(pools) == {"value", "foreign_netbuy", "institution_netbuy"}
    assert [r["symbol"] for r in pools["value"]] == ["222222", "111111"]
    assert pools["value"][0]["price"] == 300_000.0
    # 순매수 축은 metric/rank 로만 의미가 있고 부호를 유지해야 한다(병합 규칙 재구성용).
    assert pools["foreign_netbuy"][0]["metric"] == 594_000.0
    assert pools["foreign_netbuy"][0]["rank"] == 1
    assert pools["institution_netbuy"][0]["metric"] == -1_000.0
    # v1 리더 하위 호환 — 거래대금 풀은 최상위에도 미러링.
    assert [r["symbol"] for r in rec["value_pool"]] == ["222222", "111111"]


async def test_record_alt_ranking_skips_only_failed_pool(tmp_path: Path, monkeypatch) -> None:
    """한 축의 조회 실패가 나머지 축의 그날 데이터를 통째로 날리면 안 된다."""
    import json

    async def boom(_rest, **_kw):
        raise RuntimeError("kiwoom down")

    _patch_alt_pools(
        monkeypatch,
        value=boom,
        foreign=[_ranked("333333", metric=1.0, rank=1)],
        institution=None,  # 빈 풀 — 이 축도 스킵
    )
    out = tmp_path / "alt-rank.jsonl"

    path = await record_alt_ranking(
        None,  # type: ignore[arg-type]
        _discovery(chosen=["005930"], blocked=[]),
        pool_n=10,
        exclude_etf=True,
        out_path=out,
    )

    assert path == out
    rec = json.loads(out.read_text(encoding="utf-8").strip())
    assert set(rec["alt_pools"]) == {"foreign_netbuy"}
    assert "value_pool" not in rec  # 거래대금 축이 없으면 하위 호환 미러도 없다


async def test_record_alt_ranking_swallows_fetch_failure(tmp_path: Path, monkeypatch) -> None:
    """대안 순위 조회 실패는 매매를 막으면 안 된다 — None 반환 + 파일 미생성 + 예외 없음."""

    async def boom(_rest, **_kw):
        raise RuntimeError("kiwoom down")

    _patch_alt_pools(monkeypatch, value=boom, foreign=boom, institution=boom)
    out = tmp_path / "alt-rank.jsonl"

    path = await record_alt_ranking(
        None,  # type: ignore[arg-type]
        _discovery(chosen=[], blocked=[]),
        pool_n=10,
        exclude_etf=True,
        out_path=out,
    )

    assert path is None
    assert not out.exists()


async def test_record_alt_ranking_skips_empty_pool(tmp_path: Path, monkeypatch) -> None:
    """모든 대안 풀이 비면 기록하지 않는다 — 2026-07-05 pytest 가 0 라인 12줄을
    운영 파일에 오염시킨 회귀 방지(조회 실패·페이크 fetch 모두 이 분기로 걸러짐)."""
    _patch_alt_pools(monkeypatch)  # 셋 다 빈 풀
    out = tmp_path / "alt-rank.jsonl"

    path = await record_alt_ranking(
        None,  # type: ignore[arg-type]
        _discovery(chosen=["005930"], blocked=[]),
        pool_n=10,
        exclude_etf=True,
        out_path=out,
    )

    assert path is None
    assert not out.exists()


def _empty_overrides() -> PaperDayOverrides:
    return PaperDayOverrides(
        profile=None,
        top_n=3,
        liquidate="15:15",
        discover_delay_sec=30,
        include_etf=False,
        summary_msg=None,
    )


def test_build_paper_day_runner_no_heartbeat_by_default(tmp_path: Path) -> None:
    """heartbeat 인자 없이 호출하면 runner.heartbeat=None — 단위 테스트 호환."""
    runner, _ = build_paper_day_runner(
        _empty_overrides(),
        liquidate_at=time(15, 15),
        bar_minutes=1,
        allow_real=False,
        dry_run=True,
        telegram_throttle_min=0,
        today_kst=date(2026, 5, 12),
        log_dir=tmp_path / "logs",
        report_root=tmp_path / "reports",
    )
    assert runner.heartbeat is None


def test_build_paper_day_runner_wires_heartbeat_into_runner(tmp_path: Path) -> None:
    """29단계 PR27 — 5/12 회귀: cli.py 가 Heartbeat() 를 명시 주입하면 PaperRunner.heartbeat
    필드가 그대로 채워져 _heartbeat_loop 이 동작하도록 wiring 보장.
    """
    heartbeat_path = tmp_path / "heartbeat" / "runner.txt"
    hb = Heartbeat(path=heartbeat_path, stopped_path=tmp_path / "heartbeat" / "stopped.txt")
    runner, _ = build_paper_day_runner(
        _empty_overrides(),
        liquidate_at=time(15, 15),
        bar_minutes=1,
        allow_real=False,
        dry_run=True,
        telegram_throttle_min=0,
        today_kst=date(2026, 5, 12),
        log_dir=tmp_path / "logs",
        report_root=tmp_path / "reports",
        heartbeat=hb,
    )
    assert runner.heartbeat is hb
    # Heartbeat 객체가 실제 동작하는지 가벼운 sanity check (디렉터리 자동 생성 + write 가능)
    hb.write()
    assert heartbeat_path.exists()


# ---- 섀도우 세션 무장 (_try_arm_shadow) ------------------------------------

from typing import ClassVar  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

from auto_trading._common import KST as _KST  # noqa: E402
from auto_trading.daytrade import cli_helpers as _ch  # noqa: E402
from auto_trading.runner import PaperRunner  # noqa: E402


def _shadow_cand(symbol: str, name: str = "", market: str = "KOSPI") -> RankedSymbol:
    return RankedSymbol(symbol=symbol, market=market, name=name, volume=100, price=10_000.0)  # type: ignore[arg-type]


def _blocked_result(*, avoid: tuple[str, ...] = ()) -> DiscoveryResult:
    cands = [
        _shadow_cand("005930", "삼성전자"),
        _shadow_cand("000660", "SK하이닉스"),
        _shadow_cand("035420"),
    ]
    blocked = [
        (c.symbol, "avoid_symbols" if c.symbol in avoid else "KOSPI_below_sma") for c in cands
    ]
    return DiscoveryResult(
        chosen=[],
        blocked=blocked,
        gates={},
        note_ko="차단",
        names={c.symbol: c.name for c in cands if c.name},
        candidates=cands,
    )


def _day_runner(tmp_path: Path) -> PaperRunner:
    from datetime import time as _time

    return PaperRunner(liquidate_at=_time(15, 15, tzinfo=_KST))


class _FakeBalance:
    deposit = 5_000_000.0
    holdings: ClassVar[list] = []


async def test_try_arm_shadow_picks_gate_blocked_and_arms(tmp_path: Path, monkeypatch) -> None:
    """게이트 차단 후보 top_n 으로 섀도우 무장 — dry_run 강제 + universe 주입."""
    monkeypatch.chdir(tmp_path)  # tracker log_path(data/shadow) 가 cwd 상대라 격리
    monkeypatch.setattr(_ch, "get_balance", AsyncMock(return_value=_FakeBalance()))
    runner = _day_runner(tmp_path)

    armed = await _ch._try_arm_shadow(
        object(), runner, _blocked_result(), top_n=2, strategy_factory=None
    )

    assert armed is True
    assert runner.dry_run is True
    assert runner.shadow_tracker is not None
    assert runner.shadow_tracker.initial_cash == 5_000_000.0  # 실계좌 평가액 반영
    assert runner.symbols == ["005930", "000660"]  # 순위순 top_n
    assert runner._universe_ready.is_set()


async def test_try_arm_shadow_excludes_avoid_symbols(tmp_path: Path, monkeypatch) -> None:
    """회피 종목은 섀도우에서도 제외 — 실세션 행동의 재현이어야 반사실이 유효."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_ch, "get_balance", AsyncMock(return_value=_FakeBalance()))
    runner = _day_runner(tmp_path)

    armed = await _ch._try_arm_shadow(
        object(),
        runner,
        _blocked_result(avoid=("005930",)),
        top_n=2,
        strategy_factory=None,
    )

    assert armed is True
    assert runner.symbols == ["000660", "035420"]


async def test_try_arm_shadow_balance_failure_falls_back(tmp_path: Path, monkeypatch) -> None:
    """잔고 조회 실패 시 고정 가상 자본으로 폴백 — 섀도우가 막히지 않는다."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_ch, "get_balance", AsyncMock(side_effect=RuntimeError("boom")))
    runner = _day_runner(tmp_path)

    armed = await _ch._try_arm_shadow(
        object(), runner, _blocked_result(), top_n=3, strategy_factory=None
    )

    assert armed is True
    assert runner.shadow_tracker is not None
    assert runner.shadow_tracker.initial_cash == _ch._SHADOW_FALLBACK_CASH


async def test_try_arm_shadow_no_candidates_returns_false(tmp_path: Path, monkeypatch) -> None:
    """후보 자체가 없으면(발굴 실패 등) 무장하지 않고 기존 종료 경로 유지."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_ch, "get_balance", AsyncMock(return_value=_FakeBalance()))
    runner = _day_runner(tmp_path)
    empty = DiscoveryResult(chosen=[], blocked=[], gates={}, note_ko="후보 없음")

    armed = await _ch._try_arm_shadow(object(), runner, empty, top_n=3, strategy_factory=None)

    assert armed is False
    assert runner.shadow_tracker is None
    assert not runner._universe_ready.is_set()


class _FakeRestCtx:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc):
        return False


async def _run_delayed_discover(monkeypatch, tmp_path, *, shadow_on_blocked: bool) -> PaperRunner:
    monkeypatch.chdir(tmp_path)  # data/shadow 상대 경로 격리
    monkeypatch.setattr(_ch, "KiwoomRestClient", lambda: _FakeRestCtx())
    monkeypatch.setattr(_ch, "discover_and_arm", AsyncMock(return_value=_blocked_result()))
    monkeypatch.setattr(_ch, "record_alt_ranking", AsyncMock(return_value=None))
    monkeypatch.setattr(_ch, "get_balance", AsyncMock(return_value=_FakeBalance()))
    runner = _day_runner(tmp_path)
    await _ch.delayed_discover(
        runner,
        top_n=3,
        delay=0,
        shadow_on_blocked=shadow_on_blocked,
    )
    return runner


async def test_delayed_discover_blocked_arms_shadow_instead_of_stop(
    tmp_path: Path, monkeypatch
) -> None:
    """옵트인 시 게이트 전면 차단 → stop 대신 섀도우 세션으로 계속."""
    runner = await _run_delayed_discover(monkeypatch, tmp_path, shadow_on_blocked=True)
    from auto_trading.runner import RunnerControlState

    assert runner.shadow_tracker is not None
    assert runner.symbols == ["005930", "000660", "035420"]
    assert runner._control_state is not RunnerControlState.STOPPING


async def test_delayed_discover_blocked_stops_when_shadow_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    """기본(False)이면 기존 동작 그대로 — no_candidates 로 종료 (회귀 0)."""
    runner = await _run_delayed_discover(monkeypatch, tmp_path, shadow_on_blocked=False)
    from auto_trading.runner import RunnerControlState

    assert runner.shadow_tracker is None
    assert runner._control_state is RunnerControlState.STOPPING
