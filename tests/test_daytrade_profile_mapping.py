"""daytrade YAML → RiskLimits 신규 옵션 매핑 — 29단계 PR43.

apply_profile_to_runner 가 PR9/10 (max_consecutive_losses) + PR27/28
(max_position_correlation) 를 누락하던 빈틈 회복.
"""

from __future__ import annotations

from auto_trading.daytrade.cli_helpers import apply_profile_to_runner, build_profile_factory
from auto_trading.daytrade.profile import DaytradeProfile, DaytradeRiskLimits
from auto_trading.market.index import IndexMomentumTracker
from auto_trading.risk.manager import RiskManager
from auto_trading.runner import PaperRunner
from auto_trading.strategy.sma_crossover import SmaCrossover


def _runner() -> PaperRunner:
    return PaperRunner(
        strategy=SmaCrossover(),
        symbols=["005930"],
        risk=RiskManager(),
    )


def _profile(**risk: object) -> DaytradeProfile:
    return DaytradeProfile(
        name="test",
        risk=DaytradeRiskLimits(**risk),  # type: ignore[arg-type]
    )


def test_apply_profile_maps_correlation_limit() -> None:
    runner = _runner()
    apply_profile_to_runner(_profile(max_position_correlation=0.7), runner)
    assert runner.risk.limits.max_position_correlation == 0.7


def test_apply_profile_maps_consecutive_loss_limit() -> None:
    runner = _runner()
    apply_profile_to_runner(
        _profile(max_consecutive_losses=3, consecutive_loss_pause_sec=43_200), runner
    )
    assert runner.risk.limits.max_consecutive_losses == 3
    assert runner.risk.limits.consecutive_loss_pause_sec == 43_200


def test_apply_profile_defaults_keep_options_disabled() -> None:
    """YAML 에 새 옵션 안 적으면 0/default — 기존 동작 보존."""
    runner = _runner()
    apply_profile_to_runner(_profile(), runner)
    assert runner.risk.limits.max_position_correlation == 0.0
    assert runner.risk.limits.max_consecutive_losses == 0


def test_apply_profile_preserves_existing_options() -> None:
    """기존 옵션 (stop_loss_pct 등) 매핑 회귀 0."""
    runner = _runner()
    apply_profile_to_runner(
        _profile(stop_loss_pct=0.01, daily_loss_kill=True, max_position_correlation=0.7),
        runner,
    )
    assert runner.risk.limits.stop_loss_pct == 0.01
    assert runner.risk.limits.daily_loss_kill is True
    assert runner.risk.limits.max_position_correlation == 0.7


def test_apply_profile_maps_position_and_exposure_limits() -> None:
    """종목당/전체 노출 한도가 러너 limits 로 매핑된다."""
    runner = _runner()
    apply_profile_to_runner(_profile(max_position_weight=0.10, max_portfolio_exposure=0.60), runner)
    assert runner.risk.limits.max_position_weight == 0.10
    assert runner.risk.limits.max_portfolio_exposure == 0.60


def test_apply_profile_keeps_engine_defaults_when_unset() -> None:
    """미설정(None)이면 엔진 기본(0.20/0.80) 유지 — 기존 동작 보존."""
    runner = _runner()
    apply_profile_to_runner(_profile(), runner)
    assert runner.risk.limits.max_position_weight == 0.20
    assert runner.risk.limits.max_portfolio_exposure == 0.80


def test_build_factory_weight_is_one_when_position_weight_set() -> None:
    """max_position_weight 설정 시 신호 weight=1.0 (이중곱 방지)."""
    profile = _profile(max_position_weight=0.10)
    factory = build_profile_factory(profile, IndexMomentumTracker())
    strat = factory("005930")
    assert strat.params["weight"] == 1.0


def test_build_factory_weight_is_split_when_position_weight_unset() -> None:
    """미설정이면 기존 균등분할(1/top_n) 보존."""
    profile = DaytradeProfile(name="t", top_n=4)
    factory = build_profile_factory(profile, IndexMomentumTracker())
    strat = factory("005930")
    assert strat.params["weight"] == 0.25


def test_profile_validates_position_weight_range() -> None:
    """max_position_weight 0~1 범위 검증."""
    import pydantic

    try:
        DaytradeRiskLimits(max_position_weight=1.5)
    except pydantic.ValidationError:
        pass
    else:
        msg = "1.5 는 ValidationError 가 발생해야 한다"
        raise AssertionError(msg)


def test_profile_validates_correlation_range() -> None:
    """0~1 범위 검증."""
    import pydantic

    try:
        DaytradeRiskLimits(max_position_correlation=1.5)
    except pydantic.ValidationError:
        pass
    else:
        msg = "1.5 는 ValidationError 가 발생해야 한다"
        raise AssertionError(msg)


def test_profile_validates_consecutive_losses_range() -> None:
    """0~20 범위 검증."""
    import pydantic

    try:
        DaytradeRiskLimits(max_consecutive_losses=100)
    except pydantic.ValidationError:
        pass
    else:
        msg = "100 은 ValidationError 가 발생해야 한다"
        raise AssertionError(msg)
