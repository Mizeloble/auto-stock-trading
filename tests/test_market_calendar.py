"""market.calendar 회귀.

(1) 평일·주말 분기, (2) 정적 휴장일 매칭, (3) `next_trading_day` 의 휴장
연속 건너뜀, (4) `today_kst` 가 KST 기준으로 동작 (UTC 자정 직후 회귀).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from auto_trading.market.calendar import (
    KRX_HOLIDAYS,
    is_trading_day,
    next_trading_day,
    today_kst,
)


def test_weekday_is_trading_day_when_not_holiday() -> None:
    # 2026-05-08 (금) — 정상 평일
    assert is_trading_day(date(2026, 5, 8))


def test_weekend_is_not_trading_day() -> None:
    # 2026-05-09 토 / 2026-05-10 일
    assert not is_trading_day(date(2026, 5, 9))
    assert not is_trading_day(date(2026, 5, 10))


@pytest.mark.parametrize(
    "d",
    [
        date(2026, 1, 1),  # 신정
        date(2026, 2, 16),  # 설날 연휴
        date(2026, 2, 17),  # 설날
        date(2026, 2, 18),  # 설날 연휴
        date(2026, 3, 2),  # 삼일절 대체
        date(2026, 5, 5),  # 어린이날
        date(2026, 5, 25),  # 부처님오신날
        date(2026, 8, 17),  # 광복절 대체
        date(2026, 9, 24),  # 추석 연휴
        date(2026, 9, 25),  # 추석
        date(2026, 10, 5),  # 개천절 대체
        date(2026, 10, 9),  # 한글날
        date(2026, 12, 25),  # 성탄절
        date(2026, 12, 31),  # 연말 거래소 휴장
    ],
)
def test_2026_holidays_are_not_trading_days(d: date) -> None:
    assert not is_trading_day(d)


@pytest.mark.parametrize(
    "d",
    [
        date(2025, 1, 1),  # 신정
        date(2025, 6, 3),  # 21대 대선 임시휴장
        date(2025, 10, 9),  # 한글날
        date(2025, 12, 31),  # 연말 휴장
    ],
)
def test_2025_holidays_are_not_trading_days(d: date) -> None:
    assert not is_trading_day(d)


def test_unknown_year_falls_back_to_weekday_only() -> None:
    # 2027 미발표라 테이블 비어 있어도 평일은 거래일로 통과 (보수 fallback).
    assert 2027 not in KRX_HOLIDAYS
    assert is_trading_day(date(2027, 5, 6))  # 수요일
    assert not is_trading_day(date(2027, 5, 8))  # 토요일


def test_next_trading_day_skips_weekend() -> None:
    # 금요일(2026-05-08) 다음 거래일 → 월요일(2026-05-11)
    assert next_trading_day(date(2026, 5, 8)) == date(2026, 5, 11)


def test_next_trading_day_skips_chuseok_block() -> None:
    # 추석 직전 평일(2026-09-23 수) 다음 거래일 → 추석/주말 건너뛴 2026-09-28(월)
    assert next_trading_day(date(2026, 9, 23)) == date(2026, 9, 28)


def test_next_trading_day_starts_from_next_day() -> None:
    # 거래일 자체를 입력해도 +1 일부터 시작 — 같은 날 반환 X
    assert next_trading_day(date(2026, 5, 7)) == date(2026, 5, 8)


def test_today_kst_uses_seoul_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    # 2026-05-08 KST 23:30 = 2026-05-08 14:30 UTC. KST 기준 5/8 이 나와야 함.
    fake_now = datetime(2026, 5, 8, 14, 30, tzinfo=UTC)

    class _FakeDateTime:
        @staticmethod
        def now(tz: ZoneInfo | None = None) -> datetime:
            return fake_now.astimezone(tz) if tz else fake_now

    monkeypatch.setattr("auto_trading.market.calendar.datetime", _FakeDateTime)
    assert today_kst() == date(2026, 5, 8)


def test_today_kst_after_utc_midnight_is_kst_next_day(monkeypatch: pytest.MonkeyPatch) -> None:
    # UTC 0시 = KST 9시 → KST 기준 같은 날짜가 아니라 +9시간 후 같은 날.
    # 2026-05-07 18:00 UTC = 2026-05-08 03:00 KST → 5/8 이 KST 의 오늘.
    fake_now = datetime(2026, 5, 7, 18, 0, tzinfo=UTC)

    class _FakeDateTime:
        @staticmethod
        def now(tz: ZoneInfo | None = None) -> datetime:
            return fake_now.astimezone(tz) if tz else fake_now

    monkeypatch.setattr("auto_trading.market.calendar.datetime", _FakeDateTime)
    assert today_kst() == date(2026, 5, 8)
