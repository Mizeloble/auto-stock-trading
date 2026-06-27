"""작은 변환·포맷 헬퍼 — 다른 daily 서브모듈이 공유하는 순수 함수 모음.

외부 의존 없음. 로그 dict 의 느슨한 값(object) 을 안전하게 좁히거나(_as_*),
사람용 문자열(원·종목 라벨·등락률)로 바꾼다.
"""

from __future__ import annotations

from datetime import UTC
from typing import Any


def _as_float(raw: object) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return None


def _as_int(raw: object) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)  # type: ignore[call-overload]
    except TypeError, ValueError:
        return None


def _as_str(raw: object) -> str | None:
    """비어있지 않은 문자열만 통과. 그 외(None·빈문자열·비문자열)는 None."""
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


# 2026-05-11 회귀 후속 — 시계열 한 줄 빌더.
# UTC ISO 8601 → KST "HH:MM". structlog TimeStamper(fmt="iso") 출력 호환.
# 실패하면 None — 빌더가 그 이벤트를 timeline 에서 skip.
def _ts_to_kst_hhmm(raw: object) -> str | None:
    if not isinstance(raw, str) or not raw:
        return None
    from datetime import datetime, timedelta, timezone

    # structlog 의 iso 포맷은 "...Z" / "...+00:00" / 둘 다 발생 가능.
    try:
        normalized = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    kst = dt.astimezone(timezone(timedelta(hours=9)))
    return kst.strftime("%H:%M")


def _minutes_between(start_hhmm: str | None, end_hhmm: str | None) -> int | None:
    """ "HH:MM" 두 시각의 분 차이(보유시간). 같은 날 가정 — 음수면 None.

    체결 시각은 모두 같은 거래일이라 단순 분 차이로 충분. 파싱 실패·결손 시 None.
    """
    if not start_hhmm or not end_hhmm:
        return None
    try:
        sh, sm = (int(x) for x in start_hhmm.split(":"))
        eh, em = (int(x) for x in end_hhmm.split(":"))
    except ValueError, AttributeError:
        return None
    diff = (eh * 60 + em) - (sh * 60 + sm)
    return diff if diff >= 0 else None


def _format_won_int(value: object) -> str:
    """천 단위 콤마 + '원'. 변환 실패면 '?원'."""
    if value is None:
        return "?원"
    try:
        return f"{round(float(value)):,}원"  # type: ignore[arg-type]
    except TypeError, ValueError:
        return "?원"


def _with_name_label(symbol: str, names: dict[str, str]) -> str:
    """'005930 삼성전자' 형식. name 없으면 코드만."""
    name = names.get(symbol)
    return f"{symbol} {name}" if name else symbol


def _guard_pct_part(kind: str, raw: dict[str, Any]) -> str:
    """가드 이벤트의 등락률을 ' +2.33%' / ' -2.66%' 형태로. 없으면 빈 문자열.

    partial_take 는 gain_pct(이득, +), stop_loss·trailing 은 drop_pct(하락폭, -)로 라벨링.
    """
    if kind == "partial_take":
        gain = _as_float(raw.get("gain_pct"))
        return f" +{gain:.2f}%" if gain is not None else ""
    drop = _as_float(raw.get("drop_pct"))
    return f" -{drop:.2f}%" if drop is not None else ""
