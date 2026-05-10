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
    assert agg.ingest(_t(9, 0, 15, 110, 5)) is None  # high 갱신
    assert agg.ingest(_t(9, 0, 45, 95, 7)) is None  # low 갱신
    finalized = agg.ingest(_t(9, 1, 2, 105, 3))  # 다음 버킷 진입 → 이전 봉 확정

    assert finalized is not None
    assert finalized.open == 100
    assert finalized.high == 110
    assert finalized.low == 95
    assert finalized.close == 95  # 마지막 틱 직전까지 종가 = 95
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


def test_first_tick_exactly_at_minute_boundary_starts_new_bucket() -> None:
    """09:00:00 정각 첫 틱 → 봉 시작 = 09:00:00, 다음 finalize 는 09:01 진입에서.

    floor_bucket(09:00:00, 1) = 09:00 → bucket_end = 09:01. 회귀 검증.
    """
    agg = _BarAggregator(minutes=1)
    assert agg.ingest(_t(9, 0, 0, 100, 5)) is None
    finalized = agg.ingest(_t(9, 1, 0, 105, 3))
    assert finalized is not None
    assert finalized.ts.hour == 9
    assert finalized.ts.minute == 0
    assert finalized.open == 100
    assert finalized.close == 100  # 09:01 틱은 새 봉 — 이전 봉 종가는 마지막 09:00 틱.


def test_aggregator_keeps_per_symbol_buckets_independent() -> None:
    """한 종목 boundary 진입이 다른 종목 진행 봉을 finalize 하지 않는다.

    PaperRunner 는 멀티 종목을 지원 — 종목별 bucket 이 독립이어야.
    """
    agg = _BarAggregator(minutes=1)

    def _other(hour: int, minute: int, second: int, price: float) -> Tick:
        return Tick(
            symbol="OTHER",
            ts=datetime(2025, 1, 2, hour, minute, second, tzinfo=KST),
            price=Decimal(str(price)),
            volume=1,
        )

    assert agg.ingest(_t(9, 0, 5, 100)) is None  # TEST 09:00 봉 시작
    assert agg.ingest(_other(9, 0, 10, 200)) is None  # OTHER 09:00 봉 시작
    # TEST 만 다음 분 진입 → TEST 만 finalize, OTHER 봉 진행 중 유지.
    fin_test = agg.ingest(_t(9, 1, 1, 110))
    assert fin_test is not None
    assert fin_test.symbol == "TEST"
    # OTHER 가 아직 09:00 봉 안에서 다음 틱이면 finalize None.
    assert agg.ingest(_other(9, 0, 30, 205)) is None
    # OTHER 가 09:01 진입해야 OTHER 만 finalize.
    fin_other = agg.ingest(_other(9, 1, 5, 210))
    assert fin_other is not None
    assert fin_other.symbol == "OTHER"
    assert fin_other.open == 200


def test_zero_volume_tick_still_updates_ohlc() -> None:
    """volume=0 틱(체결 없는 호가 업데이트) 도 가격은 high/low/close 에 반영.

    market.feed 는 distinct tick 만 yield 하지만 volume 이 0 일 가능성이 있어
    봉이 깨지지 않는지 회귀 검증.
    """
    agg = _BarAggregator(minutes=1)
    assert agg.ingest(_t(9, 0, 5, 100, volume=10)) is None
    assert agg.ingest(_t(9, 0, 30, 120, volume=0)) is None  # high 갱신, volume 누적 X
    finalized = agg.ingest(_t(9, 1, 0, 105, volume=2))
    assert finalized is not None
    assert finalized.high == 120
    assert finalized.close == 120
    assert finalized.volume == 10  # 0 volume 누적해도 의미 없으므로 그대로.
