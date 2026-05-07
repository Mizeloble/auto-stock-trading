"""IndexMomentumTracker — 일중 지수 모멘텀 단위 테스트."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from auto_trading.market.index import (
    IndexMomentumTracker,
    fetch_latest_index_price,
    update_index_momentum,
)


def _ts(minute: int, second: int = 0) -> datetime:
    return datetime(2026, 5, 7, 9, minute, second)


def test_momentum_none_when_history_empty() -> None:
    t = IndexMomentumTracker()
    assert t.momentum("KOSPI") is None


def test_momentum_returns_none_pct_when_no_lookback_match() -> None:
    """포인트 1개만 있으면 lookback_min 분 전 데이터 없음 → return_pct=None."""
    t = IndexMomentumTracker()
    t.push("KOSPI", _ts(0), 2700.0)
    m = t.momentum("KOSPI", lookback_min=5, now=_ts(0))
    assert m is not None
    assert m.last == 2700.0
    assert m.lookback_return_pct is None


def test_momentum_calculates_pct_from_lookback_match() -> None:
    """5분 전 2700 → 현재 2727 = +1.0%."""
    t = IndexMomentumTracker()
    t.push("KOSPI", _ts(0), 2700.0)
    t.push("KOSPI", _ts(5), 2727.0)
    m = t.momentum("KOSPI", lookback_min=5, now=_ts(5))
    assert m is not None
    assert m.last == 2727.0
    assert m.lookback_return_pct is not None
    assert abs(m.lookback_return_pct - 1.0) < 1e-9


def test_momentum_picks_closest_within_tolerance() -> None:
    """target=9:00, 후보 8:59:30 / 9:00:30 — 모두 90초 안. 가장 가까운 것 선택."""
    t = IndexMomentumTracker(match_tolerance_sec=120)
    t.push("KOSPI", _ts(4, 30), 2700.0)  # 9:04:30 — target 9:05 의 30초 전
    t.push("KOSPI", _ts(2, 0), 2680.0)  # 9:02 — 너무 멀어 무시
    t.push("KOSPI", _ts(10), 2727.0)
    m = t.momentum("KOSPI", lookback_min=5, now=_ts(10))
    # target=9:05, 가장 가까운 건 9:04:30 (2700) — +1.0%
    assert m is not None
    assert abs(m.lookback_return_pct - 1.0) < 1e-9  # type: ignore[arg-type]


def test_momentum_returns_none_when_no_match_in_tolerance() -> None:
    """tolerance 안에 매칭 없으면 lookback_return_pct=None."""
    t = IndexMomentumTracker(match_tolerance_sec=60)
    t.push("KOSPI", _ts(0), 2700.0)
    t.push("KOSPI", _ts(10), 2727.0)
    # 9:05 target — 9:00 과 5분 차이, 9:10 과 5분 차이. 60초 tolerance 초과.
    m = t.momentum("KOSPI", lookback_min=5, now=_ts(10))
    assert m is not None
    assert m.lookback_return_pct is None


def test_history_pruned_after_history_minutes() -> None:
    """history_minutes=10 — 11분 전 데이터는 정리됨."""
    t = IndexMomentumTracker(history_minutes=10)
    t.push("KOSPI", _ts(0), 2700.0)
    t.push("KOSPI", _ts(11), 2727.0)
    # 9:11 시점에서 9:00 데이터는 cutoff. 9:11-10=9:01 기준.
    series = t._history["KOSPI"]
    assert all(ts >= _ts(1) for ts, _ in series)


def test_push_ignores_non_positive_price() -> None:
    t = IndexMomentumTracker()
    t.push("KOSPI", _ts(0), 0.0)
    t.push("KOSPI", _ts(1), -10.0)
    assert t._history.get("KOSPI", []) == []


def test_market_isolation_kospi_kosdaq_independent() -> None:
    """두 시장 history 가 섞이지 않는다."""
    t = IndexMomentumTracker()
    t.push("KOSPI", _ts(0), 2700.0)
    t.push("KOSDAQ", _ts(0), 800.0)
    t.push("KOSPI", _ts(5), 2727.0)
    m_kospi = t.momentum("KOSPI", lookback_min=5, now=_ts(5))
    m_kosdaq = t.momentum("KOSDAQ", lookback_min=5, now=_ts(5))
    assert m_kospi is not None and m_kospi.last == 2727.0
    assert m_kosdaq is not None and m_kosdaq.last == 800.0


# ---- fetch / update 통합 -------------------------------------------------


class _FakeClient:
    """ka20009 응답을 흉내내는 페이크. extract_rows 와 호환되는 dict 반환."""

    def __init__(self, latest_close: float | None) -> None:
        self.latest_close = latest_close
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def request(self, tr: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tr, path, body))
        if self.latest_close is None:
            return {"return_code": 0}  # 빈 응답
        return {
            "return_code": 0,
            "inds_cur_prc_daly_rept": [
                {"cur_prc_n": str(self.latest_close)},
                {"cur_prc_n": "2680"},
            ],
        }


@pytest.mark.asyncio
async def test_fetch_latest_index_price_extracts_first_row() -> None:
    client = _FakeClient(latest_close=2727.0)
    price = await fetch_latest_index_price(client, "KOSPI")  # type: ignore[arg-type]
    assert price == 2727.0
    # ka20009 호출 + 필수 파라미터 검증
    assert len(client.calls) == 1
    tr, _, body = client.calls[0]
    assert tr == "ka20009"
    assert body["mrkt_tp"] == "0"
    assert body["inds_cd"] == "001"


@pytest.mark.asyncio
async def test_fetch_returns_none_when_response_empty() -> None:
    client = _FakeClient(latest_close=None)
    price = await fetch_latest_index_price(client, "KOSPI")  # type: ignore[arg-type]
    assert price is None


@pytest.mark.asyncio
async def test_update_pushes_history_and_computes_momentum() -> None:
    """update 두 번 호출 → 두 번째에 lookback_return_pct 계산 가능."""
    tracker = IndexMomentumTracker()
    client = _FakeClient(latest_close=2700.0)
    m1 = await update_index_momentum(client, tracker, "KOSPI", lookback_min=5, now=_ts(0))  # type: ignore[arg-type]
    assert m1 is not None
    assert m1.lookback_return_pct is None  # history 부족

    client.latest_close = 2727.0
    m2 = await update_index_momentum(client, tracker, "KOSPI", lookback_min=5, now=_ts(5))  # type: ignore[arg-type]
    assert m2 is not None
    assert m2.last == 2727.0
    assert abs(m2.lookback_return_pct - 1.0) < 1e-9  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_update_returns_none_when_fetch_fails() -> None:
    tracker = IndexMomentumTracker()
    client = _FakeClient(latest_close=None)
    m = await update_index_momentum(client, tracker, "KOSPI", now=_ts(0))  # type: ignore[arg-type]
    assert m is None
    assert tracker._history.get("KOSPI", []) == []
