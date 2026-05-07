"""IndexMomentumDayTrade — 다회 진입 + 쿨다운 단위 테스트."""

from __future__ import annotations

from datetime import datetime, timedelta

from auto_trading.market.index import IndexMomentumTracker
from auto_trading.strategy.base import Bar, Side
from auto_trading.strategy.index_daytrade import IndexMomentumDayTrade


def _bar(ts: datetime, close: float = 100.0) -> Bar:
    return Bar(symbol="A", ts=ts, open=close, high=close, low=close, close=close, volume=10)


def test_first_call_fires_buy() -> None:
    s = IndexMomentumDayTrade(weight=0.5)
    sigs = s.on_bar(_bar(datetime(2026, 5, 6, 9, 0)))
    assert len(sigs) == 1
    assert sigs[0].side is Side.BUY
    assert sigs[0].weight == 0.5
    assert s.state["last_entry_ts"] == datetime(2026, 5, 6, 9, 0)


def test_no_buy_while_holding_after_first_entry() -> None:
    """첫 진입 후 청산 안 했으면(state에 last_exit_ts 없음) 다음 봉에 BUY 안 함."""
    s = IndexMomentumDayTrade()
    s.on_bar(_bar(datetime(2026, 5, 6, 9, 0)))
    sigs = s.on_bar(_bar(datetime(2026, 5, 6, 9, 1)))
    assert sigs == []


def test_no_buy_after_exit_within_cooldown() -> None:
    """청산 후 쿨다운 안에서는 BUY 안 함 (기본 30분)."""
    s = IndexMomentumDayTrade()
    s.on_bar(_bar(datetime(2026, 5, 6, 9, 0)))
    s.state["last_exit_ts"] = datetime(2026, 5, 6, 9, 5)
    # 9:25 — 청산 후 20분, 쿨다운 30분 안.
    assert s.on_bar(_bar(datetime(2026, 5, 6, 9, 25))) == []


def test_re_entry_after_cooldown_elapsed() -> None:
    """청산 + 쿨다운 경과 시 재진입 BUY."""
    s = IndexMomentumDayTrade(cooldown_min=30)
    s.on_bar(_bar(datetime(2026, 5, 6, 9, 0)))
    s.state["last_exit_ts"] = datetime(2026, 5, 6, 9, 5)
    # 9:35 — 청산 후 30분 경과.
    sigs = s.on_bar(_bar(datetime(2026, 5, 6, 9, 35)))
    assert len(sigs) == 1
    assert sigs[0].side is Side.BUY
    assert s.state["last_entry_ts"] == datetime(2026, 5, 6, 9, 35)


def test_zero_cooldown_allows_immediate_reentry() -> None:
    """cooldown_min=0 — 청산 직후 즉시 재진입."""
    s = IndexMomentumDayTrade(cooldown_min=0)
    s.on_bar(_bar(datetime(2026, 5, 6, 9, 0)))
    s.state["last_exit_ts"] = datetime(2026, 5, 6, 9, 5)
    sigs = s.on_bar(_bar(datetime(2026, 5, 6, 9, 5)))
    assert len(sigs) == 1


def test_multiple_cycles_in_one_session() -> None:
    """하루 안에 진입 → 청산 → 재진입 → 청산 → 재진입 (3회 진입)."""
    s = IndexMomentumDayTrade(cooldown_min=10)
    base = datetime(2026, 5, 6, 9, 0)
    # 1차 진입
    assert len(s.on_bar(_bar(base))) == 1
    s.state["last_exit_ts"] = base + timedelta(minutes=15)
    # 쿨다운 10분 경과 후 2차
    assert len(s.on_bar(_bar(base + timedelta(minutes=30)))) == 1
    s.state["last_exit_ts"] = base + timedelta(minutes=45)
    # 다시 쿨다운 경과 후 3차
    assert len(s.on_bar(_bar(base + timedelta(minutes=60)))) == 1


def test_reset_clears_state() -> None:
    """reset() 후 첫 호출은 새 세션처럼 BUY."""
    s = IndexMomentumDayTrade()
    s.on_bar(_bar(datetime(2026, 5, 6, 9, 0)))
    s.state["last_exit_ts"] = datetime(2026, 5, 6, 9, 5)
    s.reset()
    assert s.state == {}
    sigs = s.on_bar(_bar(datetime(2026, 5, 6, 9, 10)))
    assert len(sigs) == 1


def test_strategy_registered_in_registry() -> None:
    """레지스트리 등록 확인 — `index_momentum_daytrade` 로 조회."""
    from auto_trading.strategy import registry

    s = registry.get("index_momentum_daytrade", weight=0.33, cooldown_min=15)
    assert isinstance(s, IndexMomentumDayTrade)
    assert s.params["weight"] == 0.33
    assert s.params["cooldown_min"] == 15


# ---- 23단계 Phase F · 지수 모멘텀 게이트 결합 -------------------------------


def _tracker_with_pct(market: str, pct: float) -> IndexMomentumTracker:
    """직접 push 해서 momentum() 이 정확히 `pct`% 를 돌려주도록 만든 tracker."""
    t = IndexMomentumTracker()
    base = datetime(2026, 5, 7, 9, 0)
    t.push(market, base, 1000.0)
    t.push(market, base + timedelta(minutes=5), 1000.0 * (1.0 + pct / 100.0))
    return t


def test_momentum_gate_blocks_buy_when_pct_below_threshold() -> None:
    """게이트 활성 + 모멘텀 +0.2% < threshold +0.5% → BUY 차단."""
    tracker = _tracker_with_pct("KOSPI", 0.2)
    s = IndexMomentumDayTrade(
        momentum_threshold_pct=0.5,
        momentum_lookback_min=5,
        momentum_tracker=tracker,
        market="KOSPI",
    )
    bar = _bar(datetime(2026, 5, 7, 9, 5))
    assert s.on_bar(bar) == []


def test_momentum_gate_allows_buy_when_pct_above_threshold() -> None:
    """모멘텀 +0.8% ≥ threshold +0.5% → BUY 발사."""
    tracker = _tracker_with_pct("KOSPI", 0.8)
    s = IndexMomentumDayTrade(
        momentum_threshold_pct=0.5,
        momentum_lookback_min=5,
        momentum_tracker=tracker,
        market="KOSPI",
    )
    bar = _bar(datetime(2026, 5, 7, 9, 5))
    sigs = s.on_bar(bar)
    assert len(sigs) == 1


def test_momentum_gate_zero_threshold_means_inactive() -> None:
    """threshold=0 (기본) — tracker/market 없어도 BUY 발사."""
    s = IndexMomentumDayTrade()  # 기본
    sigs = s.on_bar(_bar(datetime(2026, 5, 7, 9, 0)))
    assert len(sigs) == 1


def test_momentum_gate_blocks_when_active_but_tracker_missing() -> None:
    """threshold > 0 인데 tracker 주입 안 됐으면 보수적 차단."""
    s = IndexMomentumDayTrade(momentum_threshold_pct=0.5)  # tracker 없음
    sigs = s.on_bar(_bar(datetime(2026, 5, 7, 9, 0)))
    assert sigs == []


def test_momentum_gate_blocks_when_history_insufficient() -> None:
    """tracker 는 있지만 lookback 데이터 없음 → 차단."""
    from auto_trading.market.index import IndexMomentumTracker

    tracker = IndexMomentumTracker()
    tracker.push("KOSPI", datetime(2026, 5, 7, 9, 5), 1000.0)  # 1포인트만
    s = IndexMomentumDayTrade(
        momentum_threshold_pct=0.5,
        momentum_tracker=tracker,
        market="KOSPI",
    )
    bar = _bar(datetime(2026, 5, 7, 9, 5))
    assert s.on_bar(bar) == []
