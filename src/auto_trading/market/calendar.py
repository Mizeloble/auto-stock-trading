"""KRX 거래일/휴장일 캘린더 — paper-day·paper·weekly-proposal 진입 가드용.

매년 12월 KRX 공식 거래일정 발표 직후 `KRX_HOLIDAYS` 의 다음 해 항목을
갱신한다. 임시휴장(예: 대선·종합훈련) 도 같은 set 에 추가하면 즉시 반영.

도메인 규칙:
- market/ 은 읽기 전용 모듈. 외부 네트워크 호출 없이 정적 테이블만 사용.
- 시간 비교는 항상 KST(`Asia/Seoul`) 기준.
- 빈 set 이거나 누락된 연도는 평일=거래일 로 보수 fallback (시동은 되지만
  빈 universe 로 즉시 종료 → 운영 영향 X).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .._common import KST

# fmt: off
KRX_HOLIDAYS: dict[int, frozenset[date]] = {
    2025: frozenset({
        date(2025, 1, 1),    # 신정
        date(2025, 1, 28),   # 임시휴장 (설 연휴)
        date(2025, 1, 29),   # 설날
        date(2025, 1, 30),   # 설날 다음날
        date(2025, 3, 3),    # 삼일절 대체 (3-1 토요일)
        date(2025, 5, 5),    # 어린이날 / 부처님오신날
        date(2025, 5, 6),    # 어린이날·부처님오신날 대체공휴일
        date(2025, 6, 3),    # 21대 대선 (임시휴장)
        date(2025, 6, 6),    # 현충일
        date(2025, 8, 15),   # 광복절
        date(2025, 10, 3),   # 개천절
        date(2025, 10, 6),   # 추석 연휴
        date(2025, 10, 7),   # 추석
        date(2025, 10, 8),   # 추석 연휴
        date(2025, 10, 9),   # 한글날
        date(2025, 12, 25),  # 성탄절
        date(2025, 12, 31),  # 연말 거래소 휴장
    }),
    2026: frozenset({
        date(2026, 1, 1),    # 신정
        date(2026, 2, 16),   # 설날 연휴
        date(2026, 2, 17),   # 설날
        date(2026, 2, 18),   # 설날 연휴
        date(2026, 3, 2),    # 삼일절 대체 (3-1 일요일)
        date(2026, 5, 5),    # 어린이날
        date(2026, 5, 25),   # 부처님오신날
        date(2026, 8, 17),   # 광복절 대체 (8-15 토요일)
        date(2026, 9, 24),   # 추석 연휴
        date(2026, 9, 25),   # 추석
        date(2026, 10, 5),   # 개천절 대체 (10-3 토요일)
        date(2026, 10, 9),   # 한글날
        date(2026, 12, 25),  # 성탄절
        date(2026, 12, 31),  # 연말 거래소 휴장
    }),
}
# fmt: on


def is_trading_day(d: date) -> bool:
    """`d` 가 KRX 거래일이면 True.

    토(weekday=5)·일(6) 자동 제외 + `KRX_HOLIDAYS` 정적 테이블 매칭.
    테이블에 없는 연도는 주말만 제외한다 (fallback 보수 운영).
    """
    if d.weekday() >= 5:
        return False
    holidays = KRX_HOLIDAYS.get(d.year, frozenset())
    return d not in holidays


def next_trading_day(d: date) -> date:
    """`d` 다음의 가장 가까운 거래일을 반환. `d` 자체가 거래일이어도 +1일부터 시작."""
    cursor = d + timedelta(days=1)
    while not is_trading_day(cursor):
        cursor += timedelta(days=1)
    return cursor


def today_kst() -> date:
    """현재 KST 기준 날짜.

    UTC 기준이면 자정 직후 한국에서 +1일이 어긋날 수 있어 KST 변환 필수.
    테스트는 `monkeypatch.setattr` 로 이 함수를 직접 교체.
    """
    return datetime.now(tz=KST).date()


__all__ = [
    "KRX_HOLIDAYS",
    "is_trading_day",
    "next_trading_day",
    "today_kst",
]
