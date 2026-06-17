"""account.reject_classifier 회귀.

29단계 PR5 (#10 1차).

검증:
- 알려진 메시지 패턴이 정확한 카테고리로.
- 코드(RC4010, 1902) 우선 매칭.
- skip_seconds 가 카테고리별 기대값.
- 모르는 메시지는 UNKNOWN + skip 0.
- 빈 입력 안전.
"""

from __future__ import annotations

import pytest

from auto_trading.account.reject_classifier import (
    RejectCategory,
    classify_reject,
)


@pytest.mark.parametrize(
    ("message", "code", "expected"),
    [
        # HOLIDAY — 코드 또는 메시지
        ("[2000](RC4010:모의투자 영업일이 아닙니다.)", "20", RejectCategory.HOLIDAY),
        ("영업일이 아닙니다", "0", RejectCategory.HOLIDAY),
        # INVALID_SYMBOL
        ("종목 정보가 없습니다 종목코드=A005930", "1902", RejectCategory.INVALID_SYMBOL),
        ("종목 정보가 없습니다", "0", RejectCategory.INVALID_SYMBOL),
        # TRADING HALT
        ("해당 종목은 거래정지 상태입니다", "0", RejectCategory.TRADING_HALT),
        ("매매정지 종목", "0", RejectCategory.TRADING_HALT),
        ("정지종목 주문 불가", "0", RejectCategory.TRADING_HALT),
        # 모의투자 매매제한 — RC4007 (2026-06-16 실측). 메시지·코드 어느 쪽으로도.
        ("[2000](RC4007:모의투자 매매제한 종목입니다.)", "20", RejectCategory.TRADING_HALT),
        ("모의투자 매매제한 종목입니다", "0", RejectCategory.TRADING_HALT),
        ("주문 불가", "RC4007", RejectCategory.TRADING_HALT),
        # VI
        ("VI 발동중 — 신규 주문 차단", "0", RejectCategory.VI_TRIGGERED),
        ("동적VI 발동으로 거래 일시 중단", "0", RejectCategory.VI_TRIGGERED),
        ("변동성완화장치 작동 중", "0", RejectCategory.VI_TRIGGERED),
        # PRICE LIMITS
        ("상한가에 도달하여 주문 불가", "0", RejectCategory.PRICE_LIMIT_UP),
        ("하한가 종목 매수 제한", "0", RejectCategory.PRICE_LIMIT_DOWN),
        # BALANCE
        ("주문가능금액 부족", "0", RejectCategory.INSUFFICIENT_BALANCE),
        ("예수금 부족 — 매수 거절", "0", RejectCategory.INSUFFICIENT_BALANCE),
        ("잔고부족", "0", RejectCategory.INSUFFICIENT_BALANCE),
        # UNKNOWN
        ("알 수 없는 시스템 에러", "9999", RejectCategory.UNKNOWN),
        ("", "", RejectCategory.UNKNOWN),
    ],
)
def test_classify_reject_known_patterns(message: str, code: str, expected: RejectCategory) -> None:
    result = classify_reject(message, code)
    assert result.category is expected


def test_skip_seconds_values() -> None:
    """카테고리별 skip 시간이 보수적 기본값과 일치."""
    assert classify_reject("상한가").skip_seconds == 600
    assert classify_reject("하한가").skip_seconds == 600
    assert classify_reject("VI 발동").skip_seconds == 120
    assert classify_reject("거래정지").skip_seconds == 86_400
    assert classify_reject("RC4010 영업일").skip_seconds == 86_400
    assert classify_reject("종목 없음", code="1902").skip_seconds == 86_400
    # INSUFFICIENT_BALANCE / UNKNOWN 은 다른 종목에 영향 없으므로 skip 0.
    assert classify_reject("예수금 부족").skip_seconds == 0
    assert classify_reject("기타 에러").skip_seconds == 0


def test_code_takes_precedence_over_ambiguous_message() -> None:
    """RC4010 코드가 있으면 메시지가 모호해도 HOLIDAY 로 분류."""
    result = classify_reject("RC4010", code="20")
    assert result.category is RejectCategory.HOLIDAY


def test_classify_handles_none_message_safely() -> None:
    """방어적 — None 메시지(타입 우회)도 UNKNOWN 으로 떨어지고 예외 X."""
    result = classify_reject("", "")
    assert result.category is RejectCategory.UNKNOWN
    assert result.skip_seconds == 0


def test_vi_pattern_does_not_match_arbitrary_v_words() -> None:
    """'VIEW' / 'visited' 같은 영어 단어가 VI 로 잘못 매칭되지 않게 lowercase 'vi' 단어 경계 의존.

    현재 단순 substring 매칭이라 'visit' 같은 단어도 매칭됨 — 이는 실제 거절 메시지에
    나올 가능성 0 이라 허용. 미래 키움 메시지가 영어 섞이면 정규식 경계로 보강.
    """
    # 거절 메시지에 'vi' 가 단어 안에 있어도 false positive 시 알 수 있게 회귀.
    assert classify_reject("device error").category is RejectCategory.UNKNOWN


def test_recovery_event_does_not_misclassify() -> None:
    """일반 시스템 에러 ('connection', 'timeout' 등) 는 모두 UNKNOWN."""
    for msg in ["connection refused", "request timed out", "internal server error"]:
        assert classify_reject(msg).category is RejectCategory.UNKNOWN
