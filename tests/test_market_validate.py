"""market.validate — 29단계 PR29 (#19) OHLCV 정합성.

각 검사 항목별 케이스: 정상 / 단일 위반 / 위치 보고 정확성.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from auto_trading._common import KST, Bar
from auto_trading.market.validate import IssueKind, validate_bars


def _bar(
    *,
    ts: datetime,
    open: float = 100.0,
    high: float = 110.0,
    low: float = 95.0,
    close: float = 105.0,
    volume: int = 1_000,
    symbol: str = "005930",
) -> Bar:
    return Bar(symbol=symbol, ts=ts, open=open, high=high, low=low, close=close, volume=volume)


def _series(n: int, *, start: datetime | None = None) -> list[Bar]:
    """정상 시계열 n 건 — 단조 증가 ts, 정상 OHLC."""
    base = start or datetime(2026, 5, 1, tzinfo=KST)
    return [_bar(ts=base + timedelta(days=i)) for i in range(n)]


# ---- 정상 케이스 -------------------------------------------------------------


def test_validate_empty_returns_no_issues() -> None:
    assert validate_bars([]) == []


def test_validate_single_bar_returns_no_issues() -> None:
    assert validate_bars([_bar(ts=datetime(2026, 5, 1, tzinfo=KST))]) == []


def test_validate_clean_series_returns_no_issues() -> None:
    assert validate_bars(_series(10)) == []


# ---- 단조성 -----------------------------------------------------------------


def test_validate_detects_ts_not_monotonic() -> None:
    bars = _series(3)
    # 1번 ts 를 0번보다 과거로 조작.
    bars[1] = _bar(ts=bars[0].ts - timedelta(days=1))
    issues = validate_bars(bars)
    kinds = [i.kind for i in issues]
    assert IssueKind.TS_NOT_MONOTONIC in kinds


def test_validate_detects_duplicate_ts() -> None:
    bars = _series(3)
    # 2번 ts 를 0번과 동일.
    bars[2] = _bar(ts=bars[0].ts)
    issues = validate_bars(bars)
    kinds = [i.kind for i in issues]
    assert IssueKind.DUPLICATE_TS in kinds


# ---- 가격·거래량 ------------------------------------------------------------


def test_validate_detects_non_positive_price() -> None:
    bars = _series(2)
    bars[1] = _bar(ts=bars[1].ts, open=0.0)
    issues = validate_bars(bars)
    assert any(i.kind is IssueKind.NON_POSITIVE_PRICE and i.index == 1 for i in issues)


def test_validate_detects_negative_volume() -> None:
    bars = _series(2)
    bars[0] = _bar(ts=bars[0].ts, volume=-100)
    issues = validate_bars(bars)
    assert any(i.kind is IssueKind.NEGATIVE_VOLUME and i.index == 0 for i in issues)


# ---- OHLC 일관성 ------------------------------------------------------------


def test_validate_detects_ohlc_high_below_close() -> None:
    """high < close 인 깨진 봉."""
    bars = _series(1)
    bars[0] = _bar(ts=bars[0].ts, open=100, high=99, low=90, close=98)
    issues = validate_bars(bars)
    assert any(i.kind is IssueKind.OHLC_INCONSISTENT for i in issues)


def test_validate_detects_ohlc_low_above_open() -> None:
    """low > open 인 깨진 봉."""
    bars = _series(1)
    bars[0] = _bar(ts=bars[0].ts, open=100, high=110, low=105, close=108)
    issues = validate_bars(bars)
    assert any(i.kind is IssueKind.OHLC_INCONSISTENT for i in issues)


def test_validate_accepts_doji_bar() -> None:
    """open=high=low=close 인 평평한 봉은 정상."""
    bars = _series(1)
    bars[0] = _bar(ts=bars[0].ts, open=100, high=100, low=100, close=100)
    issues = validate_bars(bars)
    assert all(i.kind is not IssueKind.OHLC_INCONSISTENT for i in issues)


# ---- 갭 ---------------------------------------------------------------------


def test_validate_detects_abnormal_gap_up() -> None:
    """전일 종가 100 → 다음 종가 200 (+100%) 인 비정상 갭."""
    base = datetime(2026, 5, 1, tzinfo=KST)
    bars = [
        _bar(ts=base, open=100, high=110, low=95, close=100, volume=1000),
        _bar(
            ts=base + timedelta(days=1),
            open=200,
            high=220,
            low=200,
            close=200,
            volume=1000,
        ),
    ]
    issues = validate_bars(bars, symbol="005930")
    gaps = [i for i in issues if i.kind is IssueKind.ABNORMAL_GAP]
    assert len(gaps) == 1
    assert gaps[0].index == 1
    assert "005930" in gaps[0].detail


def test_validate_detects_abnormal_gap_down() -> None:
    """-60% 갭 — 비정상."""
    base = datetime(2026, 5, 1, tzinfo=KST)
    bars = [
        _bar(ts=base, open=100, high=110, low=95, close=100, volume=1000),
        _bar(
            ts=base + timedelta(days=1),
            open=40,
            high=42,
            low=38,
            close=40,
            volume=1000,
        ),
    ]
    issues = validate_bars(bars)
    assert any(i.kind is IssueKind.ABNORMAL_GAP for i in issues)


def test_validate_accepts_normal_change() -> None:
    """±5% 정도의 일상적 변동은 갭으로 보지 않음."""
    base = datetime(2026, 5, 1, tzinfo=KST)
    bars = [
        _bar(ts=base, close=100.0),
        _bar(ts=base + timedelta(days=1), close=105.0),
    ]
    issues = validate_bars(bars)
    assert all(i.kind is not IssueKind.ABNORMAL_GAP for i in issues)


def test_validate_gap_threshold_configurable() -> None:
    """gap_threshold=0.1 으로 더 엄격하게 — +20% 도 비정상으로."""
    base = datetime(2026, 5, 1, tzinfo=KST)
    bars = [
        _bar(ts=base, close=100.0),
        _bar(ts=base + timedelta(days=1), close=120.0),  # +20%
    ]
    issues = validate_bars(bars, gap_threshold=0.1)
    assert any(i.kind is IssueKind.ABNORMAL_GAP for i in issues)


def test_validate_gap_threshold_zero_disables_check() -> None:
    """gap_threshold=0 → 갭 검사 자체 스킵."""
    base = datetime(2026, 5, 1, tzinfo=KST)
    bars = [
        _bar(ts=base, close=100.0),
        _bar(ts=base + timedelta(days=1), close=500.0),  # +400%
    ]
    issues = validate_bars(bars, gap_threshold=0.0)
    assert all(i.kind is not IssueKind.ABNORMAL_GAP for i in issues)


# ---- 복합 -------------------------------------------------------------------


def test_validate_collects_multiple_independent_issues() -> None:
    """단일 봉이 여러 위반 — 모두 보고."""
    bars = _series(1)
    bars[0] = _bar(ts=bars[0].ts, open=-10, high=-5, low=-20, close=-15, volume=-100)
    issues = validate_bars(bars)
    kinds = {i.kind for i in issues}
    assert IssueKind.NON_POSITIVE_PRICE in kinds
    assert IssueKind.NEGATIVE_VOLUME in kinds
