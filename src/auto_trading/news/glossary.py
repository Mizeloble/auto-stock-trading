"""day-trade 프로파일 파라미터의 사람말 해설.

ApplyResult.accepted 의 ``"key: old → new"`` 문자열을 받아, 라벨링한 헤드라인 +
정의(무엇) + 장점(👍) + 단점(👎) 풀이 라인을 생성한다. 텔레그램 메시지를
초보자가 읽고 변경의 의도·트레이드오프를 한 번에 이해할 수 있게 한다.

설계 메모:
- accepted 항목은 ``apply_proposal`` 이 만든 일관된 ``"{key}: {old} → {new}"``
  포맷이라 정규식 1개로 안전 파싱. rejected 항목 ("…거부…") 은 풀이 안 함.
- 풀이 대상 키는 DaytradeProposal 의 변경 가능 9개 필드 (top_n + params 3 +
  risk 5). 새 필드 추가 시 _GLOSSARY 에 1줄 추가 필요 — _glossary_covers_all
  테스트가 빠진 키를 즉시 잡아준다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class _Gloss:
    """파라미터 한 개의 사람말 해설.

    pros/cons 를 ``up`` (값을 올리는 변경) 과 ``down`` (값을 내리는 변경) 2종으로
    들고 가는 이유: "올림의 반대 = 내림" 을 자동 생성하면 문장이 어색해진다.
    """

    label: str  # 헤드라인용 사람말 명칭
    what: str  # 정의 1줄
    pros_up: str
    cons_up: str
    pros_down: str
    cons_down: str
    is_pct: bool = False  # True 면 0.018 → "1.8%" 로 표시


_GLOSSARY: dict[str, _Gloss] = {
    "top_n": _Gloss(
        label="동시 보유 종목 수",
        what="한 번에 들고 가는 종목 수 상한.",
        pros_up="더 많이 분산 — 한 종목 망해도 전체 영향 ↓",
        cons_up="종목당 자본 비중 ↓, 좋은 기회에 집중 매수 못 함",
        pros_down="좋은 기회에 집중 매수 가능",
        cons_down="한 종목 실패의 충격이 큼",
    ),
    "cooldown_min": _Gloss(
        label="재진입 대기 시간 (분)",
        what="같은 종목을 청산한 뒤 다시 살 때까지 의무 대기 시간.",
        pros_up="손절 직후 충동 재진입 차단 — 수수료·슬리피지 누적 방지",
        cons_up="진짜 좋은 재진입 기회를 놓침",
        pros_down="좋은 재진입 기회를 빨리 잡음",
        cons_down="손절 직후 또 들어가서 같은 손실을 반복할 위험",
    ),
    "momentum_lookback_min": _Gloss(
        label="모멘텀 측정 기간 (분)",
        what='지수가 "최근 N분 동안 얼마나 올랐나" 를 잴 때의 N.',
        pros_up="일시적 흔들림(노이즈)에 덜 반응, 추세 판단 안정",
        cons_up="반응이 느림, 빠른 급등 초기엔 못 잡음",
        pros_down="빠른 움직임을 즉시 감지",
        cons_down="가짜 신호에도 자주 끌림",
    ),
    "momentum_threshold_pct": _Gloss(
        label="매수 진입 모멘텀 문턱",
        what="지수가 위 기간 동안 이만큼은 올라야 매수 시작.",
        pros_up="가짜 상승 신호 차단 — 진입 종목의 질 ↑",
        cons_up="매수 자체가 줄어들어 기회를 놓침",
        pros_down="기회를 많이 잡음",
        cons_down="약한 신호에도 매수해서 오진 위험 ↑",
        is_pct=True,
    ),
    "stop_loss_pct": _Gloss(
        label="손절선",
        what="매수가 대비 N% 하락하면 무조건 매도.",
        pros_up="일시 변동에 덜 털림 — 회복 기회를 얻음",
        cons_up="발동되면 손실 절대값이 더 큼",
        pros_down="손실을 빨리 끊음 — 작게 잃고 끝",
        cons_down="노이즈에 자주 손절, 손절 횟수 누적 → 수수료↑",
        is_pct=True,
    ),
    "trailing_stop_pct": _Gloss(
        label="트레일링 스탑 폭",
        what='이익 중인 종목이 "고점 대비 N% 떨어지면" 매도.',
        pros_up="추세를 길게 탐 — 일시 되돌림에 안 털림",
        cons_up="고점 대비 이익 반납이 큼",
        pros_down="이익을 빨리 확정",
        cons_down="조금만 흔들려도 매도 — 큰 추세를 못 탐",
        is_pct=True,
    ),
    "partial_take_pct": _Gloss(
        label="부분 익절 트리거",
        what="이익이 N% 도달하면 일부 수량을 매도해 차익 챙김.",
        pros_up="큰 추세를 잡으면 이익이 커짐",
        cons_up="이익 봤다가 되돌림으로 다시 잃을 위험",
        pros_down="작은 이익도 빨리 확정",
        cons_down="추세를 못 타고 짭짤한 부분만 챙김",
        is_pct=True,
    ),
    "partial_take_qty_ratio": _Gloss(
        label="부분 익절 비중",
        what="부분 익절 시 한 번에 매도하는 수량 비율 (0~1).",
        pros_up="이익을 빨리 많이 확정 — 본전 안전",
        cons_up="남은 수량이 적어 큰 추세를 못 탐",
        pros_down="대부분 들고 가며 추세를 끝까지",
        cons_down="이익 반납 위험이 커짐",
    ),
    "daily_loss_kill": _Gloss(
        label="일중 손실 강제 청산",
        what="일중 누적 -3% 손실 도달 시 모든 포지션 강제 청산 + 그 날 매매 종료.",
        pros_up="큰 손실 차단 — 다음날 정상 가동 가능",
        cons_up="장중 반등 기회를 차단",
        pros_down="장중 반등 기회를 살림",
        cons_down="-3% 이상으로 손실이 누적될 수 있음",
    ),
}


# ``apply_proposal`` 이 만드는 정확한 포맷: ``"key: old → new"`` (key 는
# DaytradeProposal 필드명, 양쪽 공백 가능, → 좌우 토큰은 공백 없음).
_CHANGE_RE = re.compile(r"^([a-z_]+):\s*(\S+)\s*→\s*(\S+)\s*$")


def _format_value(key: str, raw: str) -> str:
    """``apply_proposal`` 이 ``str()`` 으로 직렬화한 값을 사람말로.

    - is_pct 인 키는 0.018 → "1.8%". 변환 실패 시 raw 그대로 (방어).
    - "None" → "미설정" (proposal 이 None 갱신을 보낼 일은 없지만 안전).
    """
    if raw in ("None", ""):
        return "미설정"
    g = _GLOSSARY.get(key)
    if g and g.is_pct:
        try:
            # 0.018 → "1.80" → "1.8" → "1.8%". trailing 0 만 제거해
            # 0.0025 → "0.25%" 도 정확.
            formatted = f"{float(raw) * 100:.2f}".rstrip("0").rstrip(".")
            return f"{formatted}%"
        except ValueError:
            return raw
    return raw


def _direction(key: str, old_raw: str, new_raw: str) -> str | None:
    """변경 방향. None 은 비교 불가 (raw 값을 숫자로 못 읽거나 동일 값)."""
    if key == "daily_loss_kill":
        # True → "켬" (안전 보강), False → "끔" (자유도 ↑)
        if new_raw == "True":
            return "up"
        if new_raw == "False":
            return "down"
        return None
    try:
        old_v, new_v = float(old_raw), float(new_raw)
    except ValueError:
        return None
    if new_v > old_v:
        return "up"
    if new_v < old_v:
        return "down"
    return None


def expand_change_with_glossary(line: str) -> list[str]:
    """ApplyResult.accepted 의 한 줄을 헤드라인 + 풀이 라인 리스트로 확장.

    입력 예: ``"top_n: 3 → 4"`` 또는 ``"• top_n: 3 → 4"`` (호출처 들여쓰기 허용)
    출력 예::

        ["• 동시 보유 종목 수 (top_n): 3 → 4",
         "  └ 한 번에 들고 가는 종목 수 상한.",
         "    👍 더 많이 분산 — 한 종목 망해도 전체 영향 ↓",
         "    👎 종목당 자본 비중 ↓, 좋은 기회에 집중 매수 못 함"]

    파싱 실패하거나 매핑 없는 키면 입력을 그대로 단일 라인 리스트로 반환
    (호출처가 fallback 없이 안전하게 쓸 수 있게).
    """
    body = line.removeprefix("• ").strip()
    m = _CHANGE_RE.match(body)
    if not m:
        return [line]
    key, old_raw, new_raw = m.group(1), m.group(2), m.group(3)
    g = _GLOSSARY.get(key)
    if g is None:
        return [line]
    old_fmt = _format_value(key, old_raw)
    new_fmt = _format_value(key, new_raw)
    direction = _direction(key, old_raw, new_raw)
    out = [
        f"• {g.label} ({key}): {old_fmt} → {new_fmt}",
        f"  └ {g.what}",
    ]
    if direction == "up":
        out.append(f"    👍 {g.pros_up}")
        out.append(f"    👎 {g.cons_up}")
    elif direction == "down":
        out.append(f"    👍 {g.pros_down}")
        out.append(f"    👎 {g.cons_down}")
    # direction=None (값 동일/파싱 실패) 은 정의만 남기고 종료 — 트레이드오프
    # 표시가 거짓일 위험을 피한다.
    return out


__all__ = ["expand_change_with_glossary"]
