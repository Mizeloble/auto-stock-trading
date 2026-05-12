"""risk.correlation_collector — 29단계 PR28 (#15 Phase 2).

키움 일봉 fetch + 페어 상관계수 변환. get_daily_bars 는 mock 으로 대체해
외부 호출 X.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from auto_trading._common import KST, Bar
from auto_trading.risk.correlation_collector import refresh_correlations


def _bars(symbol: str, closes: list[float]) -> list[Bar]:
    """키움 일봉은 최신→과거 순. closes 는 *시간 정순* 으로 받아 역순 변환."""
    out: list[Bar] = []
    n = len(closes)
    for i, c in enumerate(reversed(closes)):
        ts = datetime(2026, 4, 1, tzinfo=KST).replace(day=n - i)
        out.append(Bar(symbol=symbol, ts=ts, open=c, high=c, low=c, close=c, volume=0))
    return out


async def test_refresh_correlations_basic() -> None:
    """동일 시계열 두 종목 → 상관계수 ≈ 1.0."""

    async def fake_get_daily_bars(_client: object, symbol: str, **_kw: object) -> list[Bar]:
        return _bars(symbol, [100.0, 102.0, 99.0, 103.0])

    with patch(
        "auto_trading.risk.correlation_collector.get_daily_bars",
        AsyncMock(side_effect=fake_get_daily_bars),
    ):
        corr = await refresh_correlations(client=object(), symbols=["AAA", "BBB"])  # type: ignore[arg-type]

    assert ("AAA", "BBB") in corr
    assert corr[("AAA", "BBB")] == pytest.approx(1.0)


async def test_refresh_correlations_skips_fetch_failure() -> None:
    """한 종목 fetch 예외 → 해당 종목만 스킵, 나머지 페어 정상."""

    async def fake_get_daily_bars(_client: object, symbol: str, **_kw: object) -> list[Bar]:
        if symbol == "BAD":
            raise RuntimeError("network down")
        return _bars(symbol, [100.0, 102.0, 99.0, 103.0])

    with patch(
        "auto_trading.risk.correlation_collector.get_daily_bars",
        AsyncMock(side_effect=fake_get_daily_bars),
    ):
        corr = await refresh_correlations(
            client=object(),  # type: ignore[arg-type]
            symbols=["AAA", "BAD", "BBB"],
        )

    # BAD 스킵 → AAA-BBB 페어만.
    assert set(corr.keys()) == {("AAA", "BBB")}


async def test_refresh_correlations_handles_short_history() -> None:
    """일봉 1건만 있는 종목 → daily_returns 계산 불가 → 페어 X."""

    async def fake_get_daily_bars(_client: object, symbol: str, **_kw: object) -> list[Bar]:
        if symbol == "NEW":
            return _bars(symbol, [100.0])
        return _bars(symbol, [100.0, 102.0, 99.0, 103.0])

    with patch(
        "auto_trading.risk.correlation_collector.get_daily_bars",
        AsyncMock(side_effect=fake_get_daily_bars),
    ):
        corr = await refresh_correlations(client=object(), symbols=["NEW", "AAA"])  # type: ignore[arg-type]

    # NEW 는 수익률 시계열 없음 → 페어 자체 X.
    assert corr == {}


async def test_refresh_correlations_empty_input() -> None:
    """symbols 비면 외부 호출 0 + 빈 dict."""
    mock = AsyncMock()
    with patch("auto_trading.risk.correlation_collector.get_daily_bars", mock):
        corr = await refresh_correlations(client=object(), symbols=[])  # type: ignore[arg-type]
    assert corr == {}
    mock.assert_not_called()


async def test_refresh_correlations_uses_lookback_tail() -> None:
    """lookback_days=2 → 마지막 3개 종가만 사용 (변경된 결과 검증)."""

    async def fake_get_daily_bars(_client: object, symbol: str, **_kw: object) -> list[Bar]:
        # AAA: 시간 정순 [100, 999, 110, 100, 120] (앞 두 개는 무시되어야)
        # BBB: 시간 정순 [200, 999, 220, 200, 240]
        if symbol == "AAA":
            return _bars(symbol, [100.0, 999.0, 110.0, 100.0, 120.0])
        return _bars(symbol, [200.0, 999.0, 220.0, 200.0, 240.0])

    with patch(
        "auto_trading.risk.correlation_collector.get_daily_bars",
        AsyncMock(side_effect=fake_get_daily_bars),
    ):
        corr = await refresh_correlations(
            client=object(),  # type: ignore[arg-type]
            symbols=["AAA", "BBB"],
            lookback_days=2,
        )

    # 뒤 3개 종가 → 같은 패턴 → 1.0 상관.
    assert corr[("AAA", "BBB")] == pytest.approx(1.0)
