"""키움 주문 거절 메시지 분류 — 한국 시장 특이 케이스 대응.

29단계 PR5 (#10 1차) — `KiwoomError.message` 의 한국어 패턴을 카테고리로
분류해서 *후속 처리* 의 결정 근거를 제공한다. 이 모듈 자체는 *순수 함수*:
외부 호출 X, runner/notify 통합은 다음 PR 에서.

활용 시나리오 (다음 PR):
- runner 가 ``KiwoomError`` 를 잡으면 ``classify_reject(...)`` 호출.
- 결과 카테고리에 따라 종목을 ``skip_seconds`` 만큼 universe 에서 제외.
- 텔레그램 알림에 카테고리 + skip 시간 표기 — 사용자가 무슨 이유인지 즉시 인지.

매칭 우선순위는 코드 순서 — 더 구체적인 패턴이 먼저, 일반 패턴이 나중.
**모르는 메시지는 ``UNKNOWN`` + skip 0** — 안전 default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

__all__ = ["RejectCategory", "RejectClassification", "classify_reject"]

# 'VI' 양 옆이 *영문자가 아닐 때* 만 매칭 — '동적VI', 'VI 발동' 은 OK,
# 'device' / 'visit' 같은 단어 안의 'vi' 는 false positive 차단.
# Python \b 는 한글/영문 경계를 단어 경계로 인식 안 해서(\w 가 한글 포함) 직접 lookaround.
_VI_RE = re.compile(r"(?<![a-z])vi(?![a-z])", re.IGNORECASE)


class RejectCategory(StrEnum):
    """주문 거절 카테고리. 후속 처리(종목 skip / 알림 / 카운터) 의 키."""

    PRICE_LIMIT_UP = "price_limit_up"  # 상한가 도달
    PRICE_LIMIT_DOWN = "price_limit_down"  # 하한가 도달
    VI_TRIGGERED = "vi_triggered"  # 변동성완화장치(VI) 발동
    TRADING_HALT = "trading_halt"  # 거래정지 종목
    HOLIDAY = "holiday"  # 휴장(RC4010 등)
    INVALID_SYMBOL = "invalid_symbol"  # 종목코드 무효(1902 등)
    INSUFFICIENT_BALANCE = "insufficient_balance"  # 예수금/매수가능금액 부족
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RejectClassification:
    """분류 결과. 종목 skip 권장 시간을 함께 묶어서 호출자가 바로 사용."""

    category: RejectCategory
    skip_seconds: int  # 0 이면 skip 권고 X (예: INSUFFICIENT_BALANCE 는 종목과 무관)


# 카테고리별 권장 skip 시간 — 운영 경험으로 보정 가능. 보수적 기본값.
_SKIP_SECONDS: dict[RejectCategory, int] = {
    RejectCategory.PRICE_LIMIT_UP: 600,  # 10분
    RejectCategory.PRICE_LIMIT_DOWN: 600,
    RejectCategory.VI_TRIGGERED: 120,  # 2분 — VI 정적 기본 발동 시간
    RejectCategory.TRADING_HALT: 86_400,  # 1일
    RejectCategory.HOLIDAY: 86_400,  # 다음 영업일까지
    RejectCategory.INVALID_SYMBOL: 86_400,  # 코드 자체 문제 — 하루 skip 후 사용자가 점검
    RejectCategory.INSUFFICIENT_BALANCE: 0,  # 다른 종목엔 영향 X
    RejectCategory.UNKNOWN: 0,  # default 안전
}


def classify_reject(message: str, code: str = "") -> RejectClassification:
    """거절 메시지(+ 응답 코드) → 카테고리.

    매칭은 더 구체적인 패턴이 먼저. 모두 실패 시 ``UNKNOWN``.
    소문자 정규화 후 *부분 일치*. 키움 메시지가 변형돼도 핵심 단어로 잡히도록.
    """
    text = (message or "").lower()
    rc = (code or "").upper()

    # HOLIDAY — 코드 우선 (RC4010 은 실측 확인된 코드).
    if "rc4010" in rc or "rc4010" in text or "영업일" in text:
        return _make(RejectCategory.HOLIDAY)

    # INVALID_SYMBOL — 코드(1902) 또는 메시지.
    if "1902" in rc or "1902" in text or ("종목" in text and ("정보" in text or "없" in text)):
        return _make(RejectCategory.INVALID_SYMBOL)

    # 거래정지/매매정지/매매제한 — 한국 시장 특이.
    # "매매제한"·RC4007 은 모의투자에서 특정 종목 거래를 막는 케이스(실거래선 정상).
    # 종목 단위로 당일 거래 불가이므로 거래정지와 동일하게 하루 skip + 발굴 회피.
    if (
        "거래정지" in text
        or "매매정지" in text
        or "정지종목" in text
        or "매매제한" in text
        or "RC4007" in rc  # rc 는 .upper() 된 코드 — 대문자로 비교
        or "rc4007" in text
    ):
        return _make(RejectCategory.TRADING_HALT)

    # VI(변동성완화장치) 발동 — 동적/정적 둘 다 같은 카테고리. 영문은 단어 경계 필수.
    if _VI_RE.search(text) or "변동성완화" in text or "변동성 완화" in text:
        return _make(RejectCategory.VI_TRIGGERED)

    # 가격제한폭 — 상/하한가는 별도 카테고리 (skip 후 매도/매수 방향 다름).
    if "상한가" in text or "상승가" in text:
        return _make(RejectCategory.PRICE_LIMIT_UP)
    if "하한가" in text or "하락가" in text:
        return _make(RejectCategory.PRICE_LIMIT_DOWN)

    # 잔고/예수금 부족 — 종목과 무관.
    if "잔고부족" in text or "예수금" in text or "주문가능금액" in text or "부족" in text:
        return _make(RejectCategory.INSUFFICIENT_BALANCE)

    return _make(RejectCategory.UNKNOWN)


def _make(category: RejectCategory) -> RejectClassification:
    return RejectClassification(category=category, skip_seconds=_SKIP_SECONDS[category])
