"""read-only 상태 조회 명령 핸들러.

러너의 `state_snapshot()` 결과를 받아 한국어 응답 문자열로 변환만 한다.
모두 결정론적 — 외부 호출·side-effect 0.
"""

from __future__ import annotations

from datetime import datetime

from ..state import (
    RunnerStateSnapshot,
    format_balance,
    format_last_signal,
    format_pnl,
    format_positions,
    format_status,
)


def handle_status(state: RunnerStateSnapshot) -> str:
    """`/status` — 러너 가동 상태 + 종목·전략·잔고 실패 카운터."""
    return format_status(state, now=datetime.now())


def handle_balance(state: RunnerStateSnapshot) -> str:
    """`/balance` — 예수금·총평가·종목별 잔고."""
    return format_balance(state)


def handle_positions(state: RunnerStateSnapshot) -> str:
    """`/positions` — 보유 종목 요약."""
    return format_positions(state)


def handle_pnl(state: RunnerStateSnapshot) -> str:
    """`/pnl` — 당일 손익 + 누적 평가손익."""
    return format_pnl(state)


def handle_last_signal(state: RunnerStateSnapshot) -> str:
    """`/last_signal` — 종목별 최근 시그널 1건."""
    return format_last_signal(state)
