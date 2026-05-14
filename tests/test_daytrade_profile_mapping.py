"""daytrade YAML → RiskLimits 신규 옵션 매핑 — 29단계 PR43.

apply_profile_to_runner 가 PR9/10 (max_consecutive_losses) + PR27/28
(max_position_correlation) 를 누락하던 빈틈 회복.
"""

from __future__ import annotations

from auto_trading.daytrade.cli_helpers import apply_profile_to_runner
from auto_trading.daytrade.profile import DaytradeProfile, DaytradeRiskLimits
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
