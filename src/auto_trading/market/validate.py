"""OHLCV 데이터 정합성 검사 — 29단계 PR29 (#19).

무엇/왜:
- 외부 데이터 소스(키움 일/분봉 등) 가 깨지면 백테스트·전략이 *거짓 양호* 하게 나옴.
  손실은 실거래에 가서야 드러남.
- 이 모듈은 ``list[Bar]`` 를 받아 단조성·양수·OHLC 일관성·갭 등 결정적 회귀 케이스를
  검사하고 ``list[ValidationIssue]`` 로 반환. 호출자가 fail-fast 할지, 경고만 할지
  선택하도록 *판정* 만 책임진다.

검사 항목:
- ts_not_monotonic: 시계열이 엄격 증가하지 않음
- duplicate_ts: 같은 ts 두 번
- non_positive_price: open/high/low/close ≤ 0
- negative_volume: volume < 0
- ohlc_inconsistent: high < max(open, close) 또는 low > min(open, close)
- abnormal_gap: 종가가 직전 대비 ±gap_threshold (기본 50%) 초과 — 데이터 누락 또는
  미반영 액면분할 가능성

설계 원칙:
- 외부 호출 0 — 순수 함수.
- 한 항목 실패가 다른 항목 검사를 막지 않음 — 모두 독립으로 평가.
- "왜 fail 인지" 만 보고하고 *수정* 은 호출자.

어떻게 확인: ``tests/test_market_validate.py`` 의 각 케이스에서 정확한 issue 발생 확인.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .._common import Bar

__all__ = [
    "IssueKind",
    "ValidationIssue",
    "validate_bars",
]


class IssueKind(StrEnum):
    TS_NOT_MONOTONIC = "ts_not_monotonic"
    DUPLICATE_TS = "duplicate_ts"
    NON_POSITIVE_PRICE = "non_positive_price"
    NEGATIVE_VOLUME = "negative_volume"
    OHLC_INCONSISTENT = "ohlc_inconsistent"
    ABNORMAL_GAP = "abnormal_gap"


@dataclass(frozen=True)
class ValidationIssue:
    """단일 정합성 위반 사실 한 줄."""

    kind: IssueKind
    index: int  # bars 리스트 내 위치 (0-based)
    detail: str  # 사람이 읽을 수 있는 한 줄 설명


def validate_bars(
    bars: list[Bar],
    *,
    symbol: str | None = None,
    gap_threshold: float = 0.5,
) -> list[ValidationIssue]:
    """OHLCV bar 리스트 정합성 검사.

    bars 가 비어있거나 1건이면 갭/단조성 검사 스킵 (생성적으로 issue 0).
    gap_threshold = 0.5 면 종가 ±50% 갭을 비정상으로 판정 — 액면분할 1:2 라도
    수정주가(upd_stkpc_tp=1) 적용 시 정상 시계열은 ±50% 갭이 거의 없음.

    검사는 입력 순서 그대로 실시 — 키움 일봉은 보통 *최신→과거* 순이라
    호출자가 필요시 정순으로 정렬 후 넘긴다 (정렬 안 하면 ts_not_monotonic 발생).
    """
    issues: list[ValidationIssue] = []
    if not bars:
        return issues

    seen_ts: set = set()
    for i, b in enumerate(bars):
        # 가격 양수
        if b.open <= 0 or b.high <= 0 or b.low <= 0 or b.close <= 0:
            issues.append(
                ValidationIssue(
                    kind=IssueKind.NON_POSITIVE_PRICE,
                    index=i,
                    detail=(
                        f"가격 ≤ 0: open={b.open}, high={b.high}, low={b.low}, close={b.close}"
                    ),
                )
            )
        # 거래량 양수
        if b.volume < 0:
            issues.append(
                ValidationIssue(
                    kind=IssueKind.NEGATIVE_VOLUME,
                    index=i,
                    detail=f"음수 거래량: {b.volume}",
                )
            )
        # OHLC 일관성 — high ≥ max(open, close), low ≤ min(open, close)
        if b.high < max(b.open, b.close) or b.low > min(b.open, b.close):
            issues.append(
                ValidationIssue(
                    kind=IssueKind.OHLC_INCONSISTENT,
                    index=i,
                    detail=(
                        f"OHLC 불일치: open={b.open}, high={b.high}, low={b.low}, close={b.close}"
                    ),
                )
            )
        # 중복 ts
        if b.ts in seen_ts:
            issues.append(
                ValidationIssue(
                    kind=IssueKind.DUPLICATE_TS,
                    index=i,
                    detail=f"중복 timestamp: {b.ts.isoformat()}",
                )
            )
        seen_ts.add(b.ts)

    # 단조성 — 엄격 증가
    for i in range(1, len(bars)):
        if bars[i].ts <= bars[i - 1].ts:
            issues.append(
                ValidationIssue(
                    kind=IssueKind.TS_NOT_MONOTONIC,
                    index=i,
                    detail=(f"ts 비단조: {bars[i - 1].ts.isoformat()} → {bars[i].ts.isoformat()}"),
                )
            )

    # 갭 — 종가가 직전 대비 ±gap_threshold 초과
    if gap_threshold > 0:
        for i in range(1, len(bars)):
            prev_close = bars[i - 1].close
            if prev_close <= 0:
                continue
            ratio = bars[i].close / prev_close - 1.0
            if abs(ratio) > gap_threshold:
                issues.append(
                    ValidationIssue(
                        kind=IssueKind.ABNORMAL_GAP,
                        index=i,
                        detail=(
                            f"종가 갭 {ratio:+.1%}: {prev_close} → {bars[i].close} "
                            f"(임계 ±{gap_threshold:.0%}{f', symbol={symbol}' if symbol else ''})"
                        ),
                    )
                )

    return issues
