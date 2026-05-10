"""account.order_journal 회귀.

29단계 PR1 (#9 주문 idempotency 1차).

검증:
- submit/accepted/failed 가 JSONL 한 줄 씩 append 된다.
- replay() 가 기록한 이벤트를 순서대로 yield 한다.
- 손상된 라인은 스킵하고 다른 라인은 살아있다.
- 동시(코루틴) 호출이 라인 손실 없이 직렬화된다.
- new_client_id() 는 호출마다 다른 32자 hex.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path

from auto_trading._common import Side
from auto_trading.account.order_journal import OrderJournal, new_client_id
from auto_trading.account.orders import Order, OrderType


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_submit_accepted_appends_two_lines_in_order(tmp_path: Path) -> None:
    journal = OrderJournal(path=tmp_path / "state.jsonl")
    cid = "abc123"
    order = Order(symbol="005930", side=Side.BUY, quantity=10)

    journal.submit(client_id=cid, order=order)
    journal.accepted(client_id=cid, order_no="0000123")

    lines = _read_lines(journal.path)
    assert len(lines) == 2
    assert lines[0]["event"] == "submit"
    assert lines[0]["client_id"] == cid
    assert lines[0]["symbol"] == "005930"
    assert lines[0]["side"] == "buy"
    assert lines[0]["qty"] == 10
    assert lines[0]["type"] == "market"
    assert lines[0]["price"] is None
    assert lines[1]["event"] == "accepted"
    assert lines[1]["order_no"] == "0000123"
    assert lines[1]["client_id"] == cid


def test_submit_records_limit_price_as_string(tmp_path: Path) -> None:
    journal = OrderJournal(path=tmp_path / "state.jsonl")
    order = Order(
        symbol="005930",
        side=Side.SELL,
        quantity=2,
        order_type=OrderType.LIMIT,
        price=Decimal("75000"),
    )
    journal.submit(client_id="x", order=order)

    [line] = _read_lines(journal.path)
    assert line["type"] == "limit"
    assert line["price"] == "75000"


def test_failed_records_error_and_does_not_swallow(tmp_path: Path) -> None:
    journal = OrderJournal(path=tmp_path / "state.jsonl")
    journal.failed(client_id="cid", error="kiwoom 1902")

    [line] = _read_lines(journal.path)
    assert line["event"] == "failed"
    assert line["error"] == "kiwoom 1902"


def test_replay_yields_events_in_order(tmp_path: Path) -> None:
    journal = OrderJournal(path=tmp_path / "state.jsonl")
    order = Order(symbol="005930", side=Side.BUY, quantity=1)
    journal.submit(client_id="c1", order=order)
    journal.accepted(client_id="c1", order_no="O1")
    journal.submit(client_id="c2", order=order)
    journal.failed(client_id="c2", error="boom")

    events = list(journal.replay())
    assert [e["event"] for e in events] == ["submit", "accepted", "submit", "failed"]
    assert [e["client_id"] for e in events] == ["c1", "c1", "c2", "c2"]


def test_replay_on_missing_file_yields_nothing(tmp_path: Path) -> None:
    journal = OrderJournal(path=tmp_path / "nope.jsonl")
    # __post_init__ 이 부모 디렉터리는 만들지만 파일은 안 만든다.
    assert not journal.path.exists()
    assert list(journal.replay()) == []


def test_replay_skips_corrupt_line_but_keeps_others(tmp_path: Path) -> None:
    path = tmp_path / "state.jsonl"
    path.write_text(
        '{"event":"submit","client_id":"a"}\n'
        "이건 JSON 이 아님\n"
        '{"event":"accepted","client_id":"a","order_no":"X"}\n',
        encoding="utf-8",
    )
    journal = OrderJournal(path=path)
    events = list(journal.replay())
    assert [e["event"] for e in events] == ["submit", "accepted"]


def test_concurrent_appends_do_not_lose_lines(tmp_path: Path) -> None:
    """OS append-mode 가 라인 단위 atomic 임을 검증.

    20개 코루틴이 각자 submit + accepted 두 줄씩 = 40줄. 모두 살아있어야.
    """
    journal = OrderJournal(path=tmp_path / "state.jsonl")
    order = Order(symbol="005930", side=Side.BUY, quantity=1)

    async def _writer(i: int) -> None:
        cid = f"c{i}"
        journal.submit(client_id=cid, order=order)
        journal.accepted(client_id=cid, order_no=f"O{i}")

    async def _drive() -> None:
        await asyncio.gather(*(_writer(i) for i in range(20)))

    asyncio.run(_drive())

    lines = _read_lines(journal.path)
    assert len(lines) == 40
    submits = {line["client_id"] for line in lines if line["event"] == "submit"}
    assert submits == {f"c{i}" for i in range(20)}


def test_new_client_id_unique_and_hex32() -> None:
    ids = {new_client_id() for _ in range(50)}
    assert len(ids) == 50
    for cid in ids:
        assert len(cid) == 32
        int(cid, 16)  # hex 검증 — 실패 시 ValueError
