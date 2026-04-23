"""PaperRunner 의 내부 BarAggregator 검증 (순수 로직, 네트워크 없음)."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from auto_trading.market.feed import Tick
from auto_trading.runner import _BarAggregator

KST = ZoneInfo("Asia/Seoul")


def _t(hour: int, minute: int, second: int, price: float, volume: int = 1) -> Tick:
    return Tick(
        symbol="TEST",
        ts=datetime(2025, 1, 2, hour, minute, second, tzinfo=KST),
        price=Decimal(str(price)),
        volume=volume,
    )


def test_ticks_within_bucket_aggregate_open_high_low_close() -> None:
    agg = _BarAggregator(minutes=1)

    assert agg.ingest(_t(9, 0, 5, 100, 10)) is None
    assert agg.ingest(_t(9, 0, 15, 110, 5)) is None    # high 갱신
    assert agg.ingest(_t(9, 0, 45, 95, 7)) is None     # low 갱신
    finalized = agg.ingest(_t(9, 1, 2, 105, 3))         # 다음 버킷 진입 → 이전 봉 확정

    assert finalized is not None
    assert finalized.open == 100
    assert finalized.high == 110
    assert finalized.low == 95
    assert finalized.close == 95     # 마지막 틱 직전까지 종가 = 95
    assert finalized.volume == 22
    # 봉 ts 는 버킷 시작 시각 = 09:00:00
    assert finalized.ts.hour == 9 and finalized.ts.minute == 0


def test_bucket_size_respects_minutes_param() -> None:
    agg = _BarAggregator(minutes=5)
    # 09:00~09:05 버킷 내 세 틱 → finalize 없음
    assert agg.ingest(_t(9, 1, 0, 100)) is None
    assert agg.ingest(_t(9, 3, 0, 105)) is None
    # 09:05 진입 → 이전 5분봉 확정
    finalized = agg.ingest(_t(9, 5, 1, 110))
    assert finalized is not None
    assert finalized.ts.minute == 0
    assert (finalized.ts + timedelta(minutes=5)).minute == 5
