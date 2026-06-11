"""account.recovery 회귀.

29단계 PR2 (#9 2차).

검증:
- 빈 입력은 빈 리스트.
- submit 만 있고 결과 이벤트 없음 → suspect 1.
- submit + accepted → suspect 0.
- submit + failed → suspect 0.
- 여러 client_id 가 섞여도 결과 미확인 것만 추림.
- client_id 없는 이벤트는 무시.
- 알 수 없는 이벤트(향후 확장)는 무시.
"""

from __future__ import annotations

from auto_trading.account.recovery import SuspectOrder, find_suspect_orders


def _submit(cid: str, **fields: object) -> dict:
    base = {
        "ts": "2026-05-10T10:00:00+09:00",
        "event": "submit",
        "client_id": cid,
        "symbol": "005930",
        "side": "buy",
        "qty": 1,
        "type": "market",
        "price": None,
    }
    base.update(fields)
    return base


def _accepted(cid: str, order_no: str = "OK") -> dict:
    return {
        "ts": "2026-05-10T10:00:01+09:00",
        "event": "accepted",
        "client_id": cid,
        "order_no": order_no,
    }


def _failed(cid: str, error: str = "boom") -> dict:
    return {"ts": "2026-05-10T10:00:01+09:00", "event": "failed", "client_id": cid, "error": error}


def test_empty_events_yields_no_suspects() -> None:
    assert find_suspect_orders([]) == []


def test_submit_without_result_is_suspect() -> None:
    [s] = find_suspect_orders([_submit("c1")])
    assert s == SuspectOrder(
        client_id="c1",
        submitted_at="2026-05-10T10:00:00+09:00",
        symbol="005930",
        side="buy",
        quantity=1,
        order_type="market",
        price=None,
    )


def test_submit_then_accepted_is_not_suspect() -> None:
    assert find_suspect_orders([_submit("c1"), _accepted("c1")]) == []


def test_submit_then_failed_is_not_suspect() -> None:
    assert find_suspect_orders([_submit("c1"), _failed("c1", "RC4010")]) == []


def test_submit_then_unknown_stays_suspect() -> None:
    """타임아웃/5xx(unknown) 는 체결됐을 수 있다 — suspect 를 해소하지 않는다 (2026-06-10)."""
    unknown = {
        "ts": "2026-05-10T10:00:01+09:00",
        "event": "unknown",
        "client_id": "c1",
        "error": "timeout",
    }
    [s] = find_suspect_orders([_submit("c1"), unknown])
    assert s.client_id == "c1"


def test_mixed_only_unresolved_are_suspects() -> None:
    events = [
        _submit("c1"),
        _accepted("c1"),
        _submit("c2"),
        _submit("c3"),
        _failed("c3"),
        _submit("c4"),  # ← 결과 없음
    ]
    suspects = find_suspect_orders(events)
    assert {s.client_id for s in suspects} == {"c2", "c4"}


def test_event_without_client_id_is_ignored() -> None:
    events = [{"event": "submit", "symbol": "X"}, _submit("c1")]
    [s] = find_suspect_orders(events)
    assert s.client_id == "c1"


def test_unknown_event_kind_does_not_clear_submit() -> None:
    """미래에 새 이벤트 종류가 추가돼도 submit 의 미해결 상태를 잘못 청소하지 않게."""
    events = [_submit("c1"), {"event": "annotate", "client_id": "c1"}]
    [s] = find_suspect_orders(events)
    assert s.client_id == "c1"


def test_limit_order_price_preserved() -> None:
    [s] = find_suspect_orders([_submit("c1", type="limit", price="75000", qty=2, side="sell")])
    assert s.order_type == "limit"
    assert s.price == "75000"
    assert s.side == "sell"
    assert s.quantity == 2


def test_redundant_submit_kept_as_single_suspect() -> None:
    """같은 client_id 의 submit 두 번(재전송) 후 결과 없음 → suspect 1건."""
    suspects = find_suspect_orders([_submit("c1"), _submit("c1", qty=5)])
    assert len(suspects) == 1
    assert suspects[0].quantity == 5  # 마지막 submit 의 정보 유지
