"""auto-trade detect-regime CLI 회귀.

29단계 PR17.

검증:
- 일봉 fetch 성공 → 분류 결과 출력 (regime + summary + 지표 표 + 권장).
- 4가지 카테고리(TREND_UP / TREND_DOWN / RANGE / HIGH_VOL) 각 케이스.
- 일봉 빈 응답 → exit 1 (_fetch_daily_bars 가드).
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import structlog
from typer.testing import CliRunner

from auto_trading._common import KST, Bar
from auto_trading.cli import app


@pytest.fixture(autouse=True)
def _reset_structlog() -> Generator[None]:
    yield
    logging.getLogger().handlers.clear()
    structlog.reset_defaults()


def _make_bars(closes: list[float], *, atr_ratio: float = 0.01) -> list[Bar]:
    """closes 시퀀스 → Bar 리스트. high/low 는 close * (1 ± atr_ratio) 로 단순화."""
    now = datetime.now(KST).replace(hour=15, minute=30, second=0, microsecond=0)
    bars = []
    for i, c in enumerate(closes):
        ts = now - timedelta(days=len(closes) - 1 - i)
        spread = c * atr_ratio
        bars.append(
            Bar(
                symbol="069500",
                ts=ts,
                open=c,
                high=c + spread,
                low=c - spread,
                close=c,
                volume=1_000,
            )
        )
    return bars


def _invoke_with_bars(bars: list[Bar]):
    fake = AsyncMock(return_value=bars)
    with patch("auto_trading.cli.get_daily_bars", fake):
        return CliRunner().invoke(app, ["detect-regime"])


def test_trend_up_classification_outputs_green_label() -> None:
    """단조 상승 60봉 → TREND_UP."""
    closes = [10_000 + i * 60 for i in range(60)]  # 일평균 +0.6% 기울기
    result = _invoke_with_bars(_make_bars(closes, atr_ratio=0.005))
    assert result.exit_code == 0, result.output
    assert "trend_up" in result.output
    assert "상승 추세" in result.output
    assert "추세추종" in result.output  # 권장 라인


def test_trend_down_classification() -> None:
    """단조 하락 60봉 → TREND_DOWN."""
    closes = [13_600 - i * 60 for i in range(60)]
    result = _invoke_with_bars(_make_bars(closes, atr_ratio=0.005))
    assert result.exit_code == 0, result.output
    assert "trend_down" in result.output
    assert "하락" in result.output


def test_range_classification() -> None:
    """좁은 진동 60봉 → RANGE."""
    closes = [10_000 + (5 if i % 2 else -5) for i in range(60)]  # ±0.05% 진동
    result = _invoke_with_bars(_make_bars(closes, atr_ratio=0.002))
    assert result.exit_code == 0, result.output
    assert "range" in result.output


def test_high_vol_classification() -> None:
    """ATR 비율 큰 → HIGH_VOL."""
    # close 진동 폭을 크게 (±10%) → ATR / close ≈ 0.10
    closes = [10_000 + (1_000 if i % 2 else -1_000) for i in range(60)]
    result = _invoke_with_bars(_make_bars(closes, atr_ratio=0.1))
    assert result.exit_code == 0, result.output
    assert "high_vol" in result.output
    assert "고변동성" in result.output


def test_empty_bars_exits_with_error() -> None:
    """일봉 fetch 빈 응답 → _fetch_daily_bars 의 typer.Exit(1)."""
    fake = AsyncMock(return_value=[])
    with patch("auto_trading.cli.get_daily_bars", fake):
        result = CliRunner().invoke(app, ["detect-regime"])
    assert result.exit_code == 1
    assert "일봉 데이터를 가져오지 못했습니다" in result.output


def test_metrics_table_shows_slope_atr_bb() -> None:
    """지표 표에 기울기/ATR/볼린저 밴드폭/마지막 종가 표시."""
    closes = [10_000 + i * 30 for i in range(60)]
    result = _invoke_with_bars(_make_bars(closes, atr_ratio=0.005))
    assert "기울기" in result.output
    assert "ATR" in result.output
    assert "볼린저" in result.output
    assert "마지막 종가" in result.output
