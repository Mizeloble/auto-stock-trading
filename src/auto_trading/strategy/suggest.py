"""시장국면(`MarketRegime`) → 추천 `StrategyProfile` 매핑.

규칙 기반 — 같은 RegimeReport 면 항상 같은 추천이 나온다.
사용자가 YAML 의 숫자만 손보면 바로 백테스트/paper 로 돌릴 수 있다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .._common import KST
from .profile import StrategyName, StrategyProfile
from .scanner import MarketRegime, RegimeReport


@dataclass(frozen=True)
class StrategyPreset:
    strategy: StrategyName
    params: dict[str, int | float]
    weight: float
    reason_ko: str


# 시장국면별 우선 추천. 새 전략 추가 시 여기에 매핑만 더하면 된다.
PRESETS: dict[MarketRegime, StrategyPreset] = {
    MarketRegime.TREND_UP: StrategyPreset(
        strategy="sma_crossover",
        params={"short": 5, "long": 20},
        weight=1.0,
        reason_ko="단기 5/장기 20 골든크로스 추세추종 — 상승 추세에서 가장 단순한 진입.",
    ),
    MarketRegime.TREND_DOWN: StrategyPreset(
        strategy="sma_crossover",
        params={"short": 5, "long": 20},
        weight=0.3,
        reason_ko=(
            "하락 추세 — SMA 크로스 유지하되 비중 30%로 보수적. 공매도 불가하면 관망도 권장."
        ),
    ),
    MarketRegime.RANGE: StrategyPreset(
        strategy="rsi_reversion",
        params={"period": 14, "oversold": 30, "overbought": 70},
        weight=1.0,
        reason_ko="박스권 — RSI 14 기본값으로 과매도 반등·과매수 되돌림을 노린다.",
    ),
    MarketRegime.HIGH_VOL: StrategyPreset(
        strategy="rsi_reversion",
        params={"period": 7, "oversold": 25, "overbought": 75},
        weight=0.5,
        reason_ko=(
            "고변동성 — RSI 기간 7로 단축, 과매도/과매수 임계값을 더 가혹하게. "
            "비중 50%로 보수적 운용."
        ),
    ),
}


def recommend(
    report: RegimeReport,
    symbol: str,
    *,
    name: str | None = None,
    now: datetime | None = None,
) -> StrategyProfile:
    """`RegimeReport` → `StrategyProfile`. `now` 를 명시하면 created_at 결정론 보장."""
    preset = PRESETS[report.regime]
    return StrategyProfile(
        name=name or f"auto_{symbol}",
        symbol=symbol,
        strategy=preset.strategy,
        params=dict(preset.params),
        weight=preset.weight,
        notes=f"{report.summary_ko} → {preset.reason_ko}",
        created_at=now or datetime.now(tz=KST),
    )


__all__ = ["PRESETS", "StrategyPreset", "recommend"]
