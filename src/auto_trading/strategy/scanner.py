"""규칙 기반 시장국면 분류기.

최근 N봉의 종가 기울기 / ATR(변동성) / Bollinger 밴드폭을 보고
"추세 강세 / 추세 약세 / 박스 / 고변동성" 중 하나로 분류한다.

부작용 없음 — DataFrame 만 보고 판정. LLM 조언자도 같은 입력으로 호출돼야 결정론적이다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import pandas as pd

from . import indicators as ind

# 임계값 — 한국 일봉 기준 휴리스틱. 사용자 피드백으로 조정 가능.
SLOPE_TREND_THRESHOLD = 0.003  # 일평균 0.3% 이상 기울기면 추세
ATR_HIGH_VOL_THRESHOLD = 0.030  # ATR/close 3% 이상이면 고변동성
BB_WIDTH_NARROW_THRESHOLD = 0.08  # 밴드폭 8% 미만이면 박스
DEFAULT_LOOKBACK = 60


class MarketRegime(StrEnum):
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    RANGE = "range"
    HIGH_VOL = "high_vol"


@dataclass(frozen=True)
class RegimeReport:
    regime: MarketRegime
    metrics: dict[str, float]
    summary_ko: str


def _slope_pct(closes: list[float]) -> float:
    """최소제곱 기울기를 평균가로 정규화 (일/봉 비율)."""
    n = len(closes)
    if n < 2:
        return 0.0
    mean_x = (n - 1) / 2.0
    mean_y = sum(closes) / n
    num = 0.0
    den = 0.0
    for i, y in enumerate(closes):
        dx = i - mean_x
        num += dx * (y - mean_y)
        den += dx * dx
    if den == 0 or mean_y == 0:
        return 0.0
    return (num / den) / mean_y


def classify(df: pd.DataFrame, *, lookback: int = DEFAULT_LOOKBACK) -> RegimeReport:
    """일봉 DataFrame → MarketRegime.

    df 는 `bars_to_dataframe` 결과 형식 (open/high/low/close/volume, ts 인덱스 오름차순) 가정.
    데이터가 부족하면 RANGE 폴백 + summary 에 경고.
    """
    if len(df) < 20:
        return RegimeReport(
            regime=MarketRegime.RANGE,
            metrics={"bars": float(len(df))},
            summary_ko=f"데이터 부족({len(df)}봉). 최소 20봉 권장 — 박스로 폴백합니다.",
        )

    window = df.tail(lookback)
    closes = [float(x) for x in window["close"].tolist()]
    highs = [float(x) for x in window["high"].tolist()]
    lows = [float(x) for x in window["low"].tolist()]

    slope_pct = _slope_pct(closes)
    last_close = closes[-1] or 1.0
    atr_val = ind.atr(highs, lows, closes, period=14) or 0.0
    atr_pct = atr_val / last_close if last_close else 0.0
    bb = ind.bollinger(closes, period=20)
    if bb is None:
        bb_width_pct = 0.0
    else:
        mid, upper, lower = bb
        bb_width_pct = (upper - lower) / mid if mid else 0.0

    metrics = {
        "slope_pct": round(slope_pct, 6),
        "atr_pct": round(atr_pct, 6),
        "bb_width_pct": round(bb_width_pct, 6),
        "last_close": round(last_close, 2),
        "bars": float(len(window)),
    }

    if atr_pct >= ATR_HIGH_VOL_THRESHOLD:
        regime = MarketRegime.HIGH_VOL
        summary = f"고변동성 (ATR {atr_pct * 100:.2f}% / 봉) — 추세·박스 신호가 휩쓸릴 위험."
    elif slope_pct >= SLOPE_TREND_THRESHOLD:
        regime = MarketRegime.TREND_UP
        summary = f"상승 추세 (일평균 +{slope_pct * 100:.2f}%) — 추세추종 전략이 유리."
    elif slope_pct <= -SLOPE_TREND_THRESHOLD:
        regime = MarketRegime.TREND_DOWN
        summary = (
            f"하락 추세 (일평균 {slope_pct * 100:.2f}%) — 매수 보수적, 공매도 불가 시 관망 권장."
        )
    elif bb_width_pct < BB_WIDTH_NARROW_THRESHOLD:
        regime = MarketRegime.RANGE
        summary = f"박스권 (밴드폭 {bb_width_pct * 100:.2f}%) — RSI 역추세가 잘 맞을 가능성."
    else:
        regime = MarketRegime.RANGE
        summary = (
            f"방향성 약함 (기울기 {slope_pct * 100:.2f}%, 밴드폭 {bb_width_pct * 100:.2f}%) "
            "— 보수적 운용 권장."
        )

    return RegimeReport(regime=regime, metrics=metrics, summary_ko=summary)


__all__ = [
    "ATR_HIGH_VOL_THRESHOLD",
    "BB_WIDTH_NARROW_THRESHOLD",
    "DEFAULT_LOOKBACK",
    "SLOPE_TREND_THRESHOLD",
    "MarketRegime",
    "RegimeReport",
    "classify",
]
