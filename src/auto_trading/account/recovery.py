"""주문 저널 복구 분석 — 손실 의심(suspect) 주문 식별.

29단계 PR2 (#9 2차) — 봇 재시작 후 저널을 replay 해서 *송신은 됐는데
체결/실패가 미확인* 인 주문을 분리한다. 다음 PR 에서 키움 미체결 조회로
실제 상태를 확인하고 자동 매칭 + 로컬 state 동기화하는 기반.

이번 PR 의 책임은 **순수 함수**:
- 저널 replay 결과(dict 시퀀스) → suspect 리스트.
- 외부 호출 X. 키움 조회는 다음 PR 에서.

매칭 규칙:
- 같은 ``client_id`` 의 ``submit`` 이 있고 그 뒤로 ``accepted`` 또는
  ``failed`` 이벤트가 없으면 → suspect.
- 같은 ``client_id`` 의 ``submit`` 두 번(중복 송신)도 결과 이벤트가
  없으면 마지막 submit 만 suspect 로 본다 (재시도가 결과를 덮어쓸 수 있음).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

__all__ = ["SuspectOrder", "find_suspect_orders"]


@dataclass(frozen=True)
class SuspectOrder:
    """결과 미확인 주문. journal 의 submit 이벤트 한 건을 그대로 옮긴 것."""

    client_id: str
    submitted_at: str  # ISO8601 with KST offset
    symbol: str
    side: str  # "buy" / "sell"
    quantity: int
    order_type: str  # "market" / "limit"
    price: str | None  # limit 시 Decimal 문자열, market 은 None


def find_suspect_orders(events: Iterable[dict]) -> list[SuspectOrder]:
    """journal.replay() 결과를 받아 결과 미확인 주문만 반환.

    이벤트 순서는 시간순이라 가정 (저널 append-only 라 보장됨).
    """
    submits: dict[str, dict] = {}
    for event in events:
        cid = str(event.get("client_id") or "")
        if not cid:
            continue
        kind = event.get("event")
        if kind == "submit":
            submits[cid] = event
        elif kind in ("accepted", "failed"):
            submits.pop(cid, None)
        # 알 수 없는 이벤트는 무시 — 향후 확장 시 영향 없음.

    return [
        SuspectOrder(
            client_id=cid,
            submitted_at=str(submit.get("ts") or ""),
            symbol=str(submit.get("symbol") or ""),
            side=str(submit.get("side") or ""),
            quantity=int(submit.get("qty") or 0),
            order_type=str(submit.get("type") or ""),
            price=submit.get("price") if submit.get("price") is not None else None,
        )
        for cid, submit in submits.items()
    ]
