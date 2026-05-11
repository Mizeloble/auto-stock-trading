"""auto-trade orders inspect CLI 회귀.

29단계 PR16.

검증:
- 저널 파일 없음 → 안내 메시지, exit 0.
- 비어있는 저널 → "비어있음" 메시지.
- submit + accepted 만 → 모든 이벤트 표 + "결과 미확인 주문 없음".
- submit only → 표에 "(결과 미확인)" 강조 + 주의 안내.
- --suspect-only + 미확인 있음 → suspect 표.
- --suspect-only + 없음 → "결과 미확인 주문 없음".
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from pathlib import Path

import pytest
import structlog
from typer.testing import CliRunner

from auto_trading._common import Side
from auto_trading.account.order_journal import OrderJournal
from auto_trading.account.orders import Order
from auto_trading.cli import app


@pytest.fixture(autouse=True)
def _reset_structlog() -> Generator[None]:
    yield
    logging.getLogger().handlers.clear()
    structlog.reset_defaults()


def _seed_journal_resolved(path: Path) -> None:
    journal = OrderJournal(path=path)
    journal.submit(
        client_id="aaaaaaaa-resolved", order=Order(symbol="005930", side=Side.BUY, quantity=10)
    )
    journal.accepted(client_id="aaaaaaaa-resolved", order_no="ORD-1")


def _seed_journal_with_suspect(path: Path) -> None:
    journal = OrderJournal(path=path)
    journal.submit(
        client_id="aaaaaaaa-resolved", order=Order(symbol="005930", side=Side.BUY, quantity=10)
    )
    journal.accepted(client_id="aaaaaaaa-resolved", order_no="ORD-1")
    journal.submit(
        client_id="bbbbbbbb-suspect", order=Order(symbol="000660", side=Side.SELL, quantity=5)
    )
    # accepted/failed 없음 → suspect.


# ---- 파일 없음 / 비어있음 ----------------------------------------------------


def test_inspect_missing_journal_shows_guidance(isolated_cwd: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["orders", "inspect", "--journal", str(isolated_cwd / "no.jsonl")],
    )
    assert result.exit_code == 0, result.output
    assert "저널 파일 없음" in result.output


def test_inspect_empty_journal_shows_message(isolated_cwd: Path) -> None:
    journal_path = isolated_cwd / "empty.jsonl"
    journal_path.write_text("", encoding="utf-8")
    result = CliRunner().invoke(app, ["orders", "inspect", "--journal", str(journal_path)])
    assert result.exit_code == 0, result.output
    assert "비어있음" in result.output


# ---- 전체 모드 ----------------------------------------------------------------


def test_inspect_resolved_only_shows_table_and_clean_status(isolated_cwd: Path) -> None:
    journal_path = isolated_cwd / "state.jsonl"
    _seed_journal_resolved(journal_path)
    result = CliRunner().invoke(app, ["orders", "inspect", "--journal", str(journal_path)])
    assert result.exit_code == 0, result.output
    assert "submit" in result.output
    assert "accepted" in result.output
    assert "결과 미확인 주문 없음" in result.output


def test_inspect_with_suspect_highlights_in_table(isolated_cwd: Path) -> None:
    journal_path = isolated_cwd / "state.jsonl"
    _seed_journal_with_suspect(journal_path)
    result = CliRunner().invoke(app, ["orders", "inspect", "--journal", str(journal_path)])
    assert result.exit_code == 0, result.output
    assert "결과 미확인" in result.output
    # 안내 라인 — suspect 1건.
    assert "주의" in result.output
    assert "1건" in result.output
    # client_id 앞 8자만 노출.
    assert "bbbbbbbb" in result.output


# ---- --suspect-only --------------------------------------------------------


def test_inspect_suspect_only_filters_to_unresolved(isolated_cwd: Path) -> None:
    journal_path = isolated_cwd / "state.jsonl"
    _seed_journal_with_suspect(journal_path)
    result = CliRunner().invoke(
        app,
        ["orders", "inspect", "--journal", str(journal_path), "--suspect-only"],
    )
    assert result.exit_code == 0, result.output
    assert "결과 미확인 주문 (1건)" in result.output
    # suspect 의 종목·방향·수량 표시.
    assert "000660" in result.output
    assert "sell" in result.output
    assert "권장 조치" in result.output


def test_inspect_suspect_only_when_clean_shows_ok(isolated_cwd: Path) -> None:
    journal_path = isolated_cwd / "state.jsonl"
    _seed_journal_resolved(journal_path)
    result = CliRunner().invoke(
        app,
        ["orders", "inspect", "--journal", str(journal_path), "--suspect-only"],
    )
    assert result.exit_code == 0, result.output
    assert "결과 미확인 주문 없음" in result.output
