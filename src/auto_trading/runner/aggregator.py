"""Tick → 봉 집계.

`runner` 패키지 분리 전 `runner.py` 에 함께 있던 순수 집계 로직을 독립 모듈로 분리.
`PaperRunner` 의 상태를 전혀 참조하지 않는 self-contained 유닛이라 분리해도
동작·테스트 영향 없음 (tests/test_runner_aggregator.py 는 패키지 re-export 로 그대로 동작).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .._common import Bar
from ..market.feed import Tick


def _floor_bucket(ts: datetime, minutes: int) -> datetime:
    base = ts.replace(second=0, microsecond=0)
    return base - timedelta(minutes=base.minute % minutes)


@dataclass
class _BarAggregator:
    """Tick 들을 분(또는 설정된 길이)봉으로 집계."""

    minutes: int = 1
    _current: dict[str, Bar] = field(default_factory=dict)
    _bucket_end: dict[str, datetime] = field(default_factory=dict)

    def ingest(self, tick: Tick) -> Bar | None:
        """Bar 가 확정되면 그 Bar 를 반환. 진행 중이면 None."""
        bucket_end = _floor_bucket(tick.ts, self.minutes) + timedelta(minutes=self.minutes)
        prev_bar = self._current.get(tick.symbol)
        prev_end = self._bucket_end.get(tick.symbol)

        finalized: Bar | None = None
        if prev_bar is not None and prev_end is not None and tick.ts >= prev_end:
            finalized = prev_bar
            prev_bar = None

        price = float(tick.price)
        volume = max(0, tick.volume)
        if prev_bar is None:
            self._current[tick.symbol] = Bar(
                symbol=tick.symbol,
                ts=bucket_end - timedelta(minutes=self.minutes),
                open=price,
                high=price,
                low=price,
                close=price,
                volume=volume,
            )
            self._bucket_end[tick.symbol] = bucket_end
        else:
            self._current[tick.symbol] = Bar(
                symbol=prev_bar.symbol,
                ts=prev_bar.ts,
                open=prev_bar.open,
                high=max(prev_bar.high, price),
                low=min(prev_bar.low, price),
                close=price,
                volume=prev_bar.volume + volume,
            )
        return finalized
