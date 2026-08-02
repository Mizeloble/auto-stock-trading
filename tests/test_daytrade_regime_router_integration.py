"""regime 자동 라우팅 통합 — 29단계 PR41.

`route_profile_by_regime` 가 (a) KODEX 200 fake 봉으로 분류 → 매핑 프로파일 선택,
(b) fetch 실패 시 fail-safe 로 현재 프로파일 유지, (c) 매핑 파일 부재 시 fallback
까지 한 번에 검증. `select_profile_for_regime` 자체의 단위는 별도(test_strategy_regime_router.py).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path

from auto_trading._common import KST, Bar
from auto_trading.daytrade.cli_helpers import (
    RegimeRoutingResult,
    route_profile_by_regime,
)
from auto_trading.daytrade.profile import (
    DaytradeProfile,
    DaytradeRegimeRouter,
    DaytradeRiskLimits,
    DaytradeStrategyParams,
)
from auto_trading.strategy.scanner import MarketRegime


def _make_bars(*, n: int = 80, start_close: float = 30_000.0, daily_pct: float) -> list[Bar]:
    """일봉 fake — 매일 daily_pct 만큼 상승/하락. ATR 작도록 OHLC 좁게."""
    bars: list[Bar] = []
    close = start_close
    ts0 = datetime(2026, 1, 1, 9, 0, tzinfo=KST)
    for i in range(n):
        close *= 1.0 + daily_pct
        bars.append(
            Bar(
                symbol="069500",
                ts=ts0 + timedelta(days=i),
                open=close * 0.999,
                high=close * 1.002,
                low=close * 0.998,
                close=close,
                volume=1_000_000,
            )
        )
    return bars


def _write_profile(
    path: Path,
    *,
    name: str,
    regime_router_enabled: bool = False,
) -> None:
    profile = DaytradeProfile(
        name=name,
        strategy="intraday_momentum",
        top_n=3,
        liquidate="15:15",
        discover_delay_sec=60,
        exclude_etf=True,
        params=DaytradeStrategyParams(),
        risk=DaytradeRiskLimits(),
        regime_router=DaytradeRegimeRouter(enabled=regime_router_enabled),
    )
    profile.dump_yaml(path)


def test_routes_to_defensive_when_classifier_returns_trend_down(tmp_path: Path) -> None:
    """KODEX 200 하락 봉 → TREND_DOWN → daytrade-defensive.yaml 선택."""
    current = tmp_path / "daytrade-current.yaml"
    _write_profile(current, name="current")
    # 매핑 파일들 미리 생성 (내용은 비어있어도 select_profile_for_regime 가 존재만 체크).
    for n in ("daytrade-trend", "daytrade-defensive", "daytrade-range", "daytrade-highvol"):
        (tmp_path / f"{n}.yaml").write_text("risk: {}\n", encoding="utf-8")

    bars = _make_bars(daily_pct=-0.01)  # 매일 -1% → TREND_DOWN 충족

    result = route_profile_by_regime(current, fetch_daily_bars=lambda _sym: bars)

    assert isinstance(result, RegimeRoutingResult)
    assert result.routed is True
    assert result.regime == MarketRegime.TREND_DOWN
    assert result.profile_path.name == "daytrade-defensive.yaml"
    assert "하락 추세" in result.summary_ko
    assert "👍" in result.summary_ko and "👎" in result.summary_ko


def test_routes_to_trend_when_classifier_returns_trend_up(tmp_path: Path) -> None:
    current = tmp_path / "daytrade-current.yaml"
    _write_profile(current, name="current")
    for n in ("daytrade-trend", "daytrade-defensive", "daytrade-range", "daytrade-highvol"):
        (tmp_path / f"{n}.yaml").write_text("risk: {}\n", encoding="utf-8")

    bars = _make_bars(daily_pct=0.01)  # 매일 +1% → TREND_UP

    result = route_profile_by_regime(current, fetch_daily_bars=lambda _sym: bars)

    assert result.routed is True
    assert result.regime == MarketRegime.TREND_UP
    assert result.profile_path.name == "daytrade-trend.yaml"


def test_fail_safe_when_fetch_raises(tmp_path: Path) -> None:
    """fetch 가 예외 → routed=False, 현재 프로파일 유지, summary 에 경고."""
    current = tmp_path / "daytrade-current.yaml"
    _write_profile(current, name="current")

    def boom(_sym: str) -> Iterable[Bar]:
        raise RuntimeError("KODEX 200 fetch 실패")

    result = route_profile_by_regime(current, fetch_daily_bars=boom)

    assert result.routed is False
    assert result.regime is None
    assert result.profile_path == current
    assert "분류 실패" in result.summary_ko


def test_fallback_when_mapped_profile_missing(tmp_path: Path) -> None:
    """매핑된 파일이 없으면 현재 프로파일 유지 + routed=False."""
    current = tmp_path / "daytrade-current.yaml"
    _write_profile(current, name="current")
    # 매핑 파일을 일부러 만들지 않음.

    bars = _make_bars(daily_pct=0.01)
    result = route_profile_by_regime(current, fetch_daily_bars=lambda _sym: bars)

    assert result.routed is False
    assert result.regime == MarketRegime.TREND_UP  # 분류는 됐음
    assert result.profile_path == current
    assert "매핑 프로파일이 없어" in result.summary_ko


def test_real_yaml_profiles_load_without_validation_error() -> None:
    """저장소의 daytrade-{trend,defensive,range,highvol,crash}.yaml 가 schema 통과 확인."""
    root = Path(__file__).resolve().parent.parent / "strategies" / "profiles"
    for name in (
        "daytrade-trend",
        "daytrade-defensive",
        "daytrade-range",
        "daytrade-highvol",
        "daytrade-crash",
    ):
        path = root / f"{name}.yaml"
        assert path.exists(), f"{path} 미존재 — 신규 프로파일 누락"
        profile = DaytradeProfile.load_yaml(path)
        # 각 프로파일은 baseline(current) 와 동일한 회전율 통제 필드를 유지해야 한다.
        assert profile.risk.daily_loss_kill is True
        assert profile.risk.max_consecutive_losses == 3
        # 2026-07-04 회귀 방지 — 포지션·노출 한도 누락 시 엔진 기본(20%/80%)이 적용돼
        # 보수적이어야 할 레짐 프로파일이 오히려 느슨해진다(defensive 실제 사례).
        # 2026-08-02 부터 crash 도 명시(이중 안전망 정합) — 예외 없이 전 프로파일 검사.
        assert profile.risk.max_position_weight is not None, f"{name}: 종목당 비중 한도 누락"
        assert profile.risk.max_portfolio_exposure is not None, f"{name}: 전체 노출 한도 누락"


# 참고: 과거엔 운영 daytrade-current.yaml 의 regime_router 가 기본 OFF 인지 단정하는
# 테스트가 여기 있었으나, 이 파일은 사용자가 자유롭게 켜고 끄는 운영 자산(.gitignore)이라
# 사용자가 명시적으로 켜면(2026-05-28 사용자 요청) 항상 빨강이 됐다. 그 테스트가 지키려던
# 진짜 보증 — "LLM 주간 자동화가 실수로 regime_router 를 켜지 못한다" — 은 운영 파일과
# 무관한 결정론적 단위 테스트로 옮겼다: test_profile_writer.py 의
# test_apply_proposal_preserves_regime_router_block.
