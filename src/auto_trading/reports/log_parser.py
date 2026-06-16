"""paper / paper-day 러너가 남긴 일자별 로그 파일을 읽어 관심 이벤트만 추출.

`logs/paper-day-YYYY-MM-DD.log` 같은 파일은 structlog JSON 라인 + 비-JSON 헤더(rich
콘솔 출력) 가 섞여 있다. JSON 라인만 파싱하고 나머진 조용히 skip.

리포트 빌더(`daily.build_daily_report`) 가 이 결과를 RunnerStateSnapshot 과 합쳐 최종
DailyReport 를 만든다.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class LogEvents:
    """로그 한 파일에서 뽑은 이벤트 묶음. 빌더 입력용."""

    discovery: dict[str, Any] | None = None  # daytrade.discovery (있으면 1건)
    rank: dict[str, Any] | None = None  # market.rank.top_volume (있으면 1건)
    # 데이터 기반 전략 수립용 일일 맥락(둘 다 하루 1건, 세션 시작 시점). 빌더가
    # DailyReport 최상위 regime/breadth_* 필드로 끌어올린다. 없으면 None.
    breadth_gate: dict[str, Any] | None = None  # daytrade.breadth_gate
    regime: dict[str, Any] | None = None  # daytrade.regime
    market_gates: list[dict[str, Any]] = field(default_factory=list)  # market.index.gate
    orders_simulated: list[dict[str, Any]] = field(default_factory=list)  # runner.order_simulated
    orders_accepted: list[dict[str, Any]] = field(default_factory=list)  # runner.order_accepted
    orders_failed: list[dict[str, Any]] = field(default_factory=list)  # runner.order_failed
    liquidations_simulated: list[dict[str, Any]] = field(
        default_factory=list
    )  # runner.liquidate_simulated
    liquidations_accepted: list[dict[str, Any]] = field(
        default_factory=list
    )  # runner.liquidate_accepted
    liquidations_failed: list[dict[str, Any]] = field(
        default_factory=list
    )  # runner.liquidate_failed
    signal_rejected: list[dict[str, Any]] = field(default_factory=list)  # runner.signal_rejected
    # 2026-05-11 회귀 후속 — 일일 리포트에 시계열 표시용. 부분 익절·손절·트레일링·
    # kill switch·청산 시작 이벤트는 reports/daily.py 의 timeline 빌더가 매수/매도/청산
    # 체결과 합쳐 시각 순으로 정렬한다.
    partial_takes: list[dict[str, Any]] = field(default_factory=list)  # position_guard.partial_take
    # 2026-06-15 회귀 후속 — 손절·트레일링 청산 가드 이벤트. 매도 체결(orders side=sell)을
    # timeline 에서 사유(손절/트레일링)로 라벨링하기 위해 파싱한다.
    stop_losses: list[dict[str, Any]] = field(default_factory=list)  # position_guard.stop_loss
    trailings: list[dict[str, Any]] = field(default_factory=list)  # position_guard.trailing
    kill_switches: list[dict[str, Any]] = field(
        default_factory=list
    )  # runner.kill_switch_triggered
    liquidate_starts: list[dict[str, Any]] = field(default_factory=list)  # runner.liquidate_start
    balance_failures: int = 0  # runner.balance_fetch_failed 누적 카운트
    fatal: list[dict[str, Any]] = field(default_factory=list)  # runner.balance_failed_fatal 등
    # daytrade.discovery / universe_chosen 로그의 names 필드 통합 — 코드→종목명.
    # 같은 코드가 두 라인에서 모두 보이면 마지막 비어있지 않은 값으로 갱신.
    symbol_names: dict[str, str] = field(default_factory=dict)


_INTEREST: dict[str, str] = {
    "daytrade.discovery": "discovery",
    "daytrade.breadth_gate": "breadth_gate",
    "daytrade.regime": "regime",
    "daytrade.universe_chosen": "_universe_chosen",
    "market.rank.top_volume": "rank",
    "market.index.gate": "market_gates",
    "runner.order_simulated": "orders_simulated",
    "runner.order_accepted": "orders_accepted",
    "runner.order_failed": "orders_failed",
    "runner.liquidate_simulated": "liquidations_simulated",
    "runner.liquidate_accepted": "liquidations_accepted",
    "runner.liquidate_failed": "liquidations_failed",
    "runner.signal_rejected": "signal_rejected",
    "position_guard.partial_take": "partial_takes",
    "position_guard.stop_loss": "stop_losses",
    "position_guard.trailing": "trailings",
    "runner.kill_switch_triggered": "kill_switches",
    "runner.liquidate_start": "liquidate_starts",
    "runner.balance_fetch_failed": "_balance_failed",
    "runner.balance_failed_fatal": "fatal",
}


def parse_log(path: Path | str) -> LogEvents:
    """파일을 한 번만 훑어 LogEvents 채워 반환.

    파일이 없거나 읽기 실패하면 빈 LogEvents — 호출자가 모든 필드 비어있는 케이스를
    "그날 데이터 없음" 으로 처리.
    """
    p = Path(path)
    events = LogEvents()
    if not p.exists():
        return events
    try:
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                _ingest_line(events, line)
    except OSError:
        return events
    return events


def _ingest_line(events: LogEvents, line: str) -> None:
    line = line.strip()
    if not line or line[0] != "{":
        return
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return
    event_name = record.get("event")
    if not isinstance(event_name, str):
        return
    target = _INTEREST.get(event_name)
    if target is None:
        return
    if target == "_balance_failed":
        # 누적 카운트만 — 마지막 consecutive 값으로 갱신.
        consecutive = record.get("consecutive")
        if isinstance(consecutive, int):
            events.balance_failures = max(events.balance_failures, consecutive)
        return
    if target == "discovery":
        events.discovery = record
        _absorb_names(events, record.get("names"))
        return
    if target == "_universe_chosen":
        # universe_chosen 자체는 따로 저장하지 않는다 — names 만 흡수.
        _absorb_names(events, record.get("names"))
        return
    if target == "rank":
        events.rank = record
        return
    if target == "breadth_gate":
        events.breadth_gate = record
        return
    if target == "regime":
        events.regime = record
        return
    bucket = getattr(events, target)
    if isinstance(bucket, list):
        bucket.append(record)


def _absorb_names(events: LogEvents, raw: object) -> None:
    """names dict 가 있으면 비어있지 않은 값으로 events.symbol_names 갱신."""
    if not isinstance(raw, dict):
        return
    for sym, name in raw.items():
        if not isinstance(sym, str) or not isinstance(name, str):
            continue
        if name.strip():
            events.symbol_names[sym] = name.strip()


def parse_lines(lines: Iterable[str]) -> LogEvents:
    """테스트용 — 파일 대신 라인 iterable 을 직접 파싱."""
    events = LogEvents()
    for line in lines:
        _ingest_line(events, line)
    return events


__all__ = ["LogEvents", "parse_lines", "parse_log"]
