"""실 모의투자 API 왕복 테스트 (KIWOOM_APP_KEY 필요).

`uv run pytest -m integration` 로만 실행. 키가 없으면 conftest 에서 모듈 skip.
"""

from __future__ import annotations

from datetime import date
from itertools import pairwise

import pytest

from auto_trading.account.balance import get_balance
from auto_trading.kiwoom.rest import KiwoomRestClient
from auto_trading.market.chart import get_daily_bars
from auto_trading.market.quote import get_quote

pytestmark = pytest.mark.integration


async def test_current_quote_samsung_electronics() -> None:
    async with KiwoomRestClient() as client:
        q = await get_quote(client, "005930")

    assert q.symbol == "005930"
    assert q.price > 0
    assert q.volume >= 0


async def test_daily_bars_paginate() -> None:
    async with KiwoomRestClient() as client:
        bars = await get_daily_bars(client, "005930", base_dt=date.today(), max_pages=2)

    assert len(bars) > 0
    first = bars[0]
    assert first.symbol == "005930"
    assert first.high >= first.low
    # 정렬: ts 오름차순 또는 내림차순 어느 쪽이든 단조
    ts_sorted_asc = all(a.ts <= b.ts for a, b in pairwise(bars))
    ts_sorted_desc = all(a.ts >= b.ts for a, b in pairwise(bars))
    assert ts_sorted_asc or ts_sorted_desc


async def test_balance_shape() -> None:
    async with KiwoomRestClient() as client:
        snap = await get_balance(client)

    # 모의투자 계정은 최초 보유 없을 수 있음 — 타입만 검증
    assert snap.deposit >= 0
    assert isinstance(snap.holdings, list)
