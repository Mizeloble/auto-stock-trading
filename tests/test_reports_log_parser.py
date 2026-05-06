"""log_parser — paper-day 로그 JSON 라인에서 관심 이벤트만 추출."""

from __future__ import annotations

import json
from pathlib import Path

from auto_trading.reports.log_parser import LogEvents, parse_lines, parse_log


def _line(event: str, **fields: object) -> str:
    record: dict[str, object] = {
        "event": event,
        "level": "info",
        "timestamp": "2026-05-05T12:00:00Z",
    }
    record.update(fields)
    return json.dumps(record)


def test_parse_skips_non_json_and_unknown_events() -> None:
    """rich 콘솔 헤더(비-JSON) 와 관심 없는 이벤트는 조용히 skip."""
    lines = [
        "키움 모드: mock",  # 비-JSON 헤더
        "",
        _line("kiwoom.token_issued", token_preview="abcd…"),  # 관심 없음
        _line("daytrade.discovery", chosen=["A", "B"], blocked=[("C", "kospi_below_sma")]),
        "rich console output without json",
    ]
    events = parse_lines(lines)
    assert events.discovery is not None
    assert events.discovery["chosen"] == ["A", "B"]


def test_parse_extracts_each_event_type_into_correct_bucket() -> None:
    lines = [
        _line(
            "market.rank.top_volume",
            chosen=[["A", "KOSPI", 1000], ["B", "KOSDAQ", 800]],
            excluded_count=53,
        ),
        _line(
            "market.index.gate", market="KOSPI", last=6900.0, sma5=6700.0, sma20=6200.0, above=True
        ),
        _line(
            "market.index.gate", market="KOSDAQ", last=1200.0, sma5=1180.0, sma20=1150.0, above=True
        ),
        _line("runner.order_simulated", symbol="A", side="buy", qty=10, price=100.0),
        _line("runner.order_simulated", symbol="B", side="buy", qty=5, price=200.0),
        _line("runner.order_accepted", symbol="C", side="buy", qty=3, order_no="x1"),
        _line("runner.order_failed", symbol="D", side="buy", qty=1, error="x"),
        _line("runner.liquidate_simulated", symbol="A", side="sell", qty=10),
        _line("runner.liquidate_accepted", symbol="C", qty=3, order_no="x2"),
        _line("runner.liquidate_failed", symbol="X", qty=1, error="boom"),
        _line("runner.signal_rejected", symbol="A", side="buy", reason="cap"),
        _line("runner.balance_fetch_failed", consecutive=2),
        _line("runner.balance_fetch_failed", consecutive=4),
        _line("runner.balance_failed_fatal", consecutive=5),
    ]
    events = parse_lines(lines)
    assert events.rank is not None
    assert len(events.market_gates) == 2
    assert len(events.orders_simulated) == 2
    assert len(events.orders_accepted) == 1
    assert len(events.orders_failed) == 1
    assert len(events.liquidations_simulated) == 1
    assert len(events.liquidations_accepted) == 1
    assert len(events.liquidations_failed) == 1
    assert len(events.signal_rejected) == 1
    assert events.balance_failures == 4
    assert len(events.fatal) == 1


def test_parse_log_missing_file_returns_empty_events(tmp_path: Path) -> None:
    """파일이 없으면 모든 필드가 비어있는 LogEvents — 호출자 안전 분기용."""
    events = parse_log(tmp_path / "does-not-exist.log")
    assert isinstance(events, LogEvents)
    assert events.discovery is None
    assert events.rank is None
    assert events.market_gates == []
    assert events.orders_simulated == []


def test_parse_log_reads_real_file(tmp_path: Path) -> None:
    """파일 I/O 라운드트립 — write 후 parse 결과가 in-memory parse_lines 와 동일."""
    path = tmp_path / "paper-day-2026-05-05.log"
    path.write_text(
        "\n".join(
            [
                "키움 모드: mock",
                _line("daytrade.discovery", chosen=["018880"], blocked=[]),
                _line("runner.order_simulated", symbol="018880", side="buy", qty=10, price=5000.0),
            ]
        ),
        encoding="utf-8",
    )
    events = parse_log(path)
    assert events.discovery is not None
    assert events.discovery["chosen"] == ["018880"]
    assert len(events.orders_simulated) == 1


def test_discovery_names_absorbed_into_symbol_names() -> None:
    """daytrade.discovery 의 names dict 가 LogEvents.symbol_names 로 흡수."""
    lines = [
        _line(
            "daytrade.discovery",
            chosen=["005930", "001500"],
            blocked=[],
            names={"005930": "삼성전자", "001500": "한솔홀딩스"},
        ),
    ]
    events = parse_lines(lines)
    assert events.symbol_names == {"005930": "삼성전자", "001500": "한솔홀딩스"}


def test_universe_chosen_names_supplement_discovery() -> None:
    """universe_chosen 의 names 가 discovery 누락분을 보강. 둘 다 있으면 마지막 비어있지 않은 값."""
    lines = [
        _line(
            "daytrade.discovery",
            chosen=["005930"],
            blocked=[],
            names={"005930": "", "001500": "한솔홀딩스"},  # 005930 빈 이름은 무시
        ),
        _line(
            "daytrade.universe_chosen",
            symbols=["005930"],
            names={"005930": "삼성전자"},  # 보강
        ),
    ]
    events = parse_lines(lines)
    assert events.symbol_names == {"005930": "삼성전자", "001500": "한솔홀딩스"}


def test_missing_names_field_is_safe() -> None:
    """names 필드가 없는 (구) discovery 로그도 깨지지 않음 — 호환성."""
    lines = [_line("daytrade.discovery", chosen=["A"], blocked=[])]
    events = parse_lines(lines)
    assert events.symbol_names == {}
    assert events.discovery is not None


def test_malformed_json_lines_are_skipped() -> None:
    """JSON 으로 시작하지만 깨진 라인도 조용히 skip — 트레이딩 보고를 막지 않게."""
    lines = [
        '{"event": "daytrade.discovery", "chosen":',  # 잘린 JSON
        _line("daytrade.discovery", chosen=["A"]),
    ]
    events = parse_lines(lines)
    assert events.discovery is not None
    assert events.discovery["chosen"] == ["A"]
