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
)
from auto_trading.daytrade.profile import (
    DaytradeProfile,
    DaytradeRiskLimits,
    DaytradeStrategyParams,
)
from auto_trading.heartbeat import Heartbeat


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
