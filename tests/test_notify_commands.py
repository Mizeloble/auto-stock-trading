"""notify/commands/ 패키지 단위 테스트.

기존 통합 테스트(test_telegram_commands.py)는 long-poll + dispatch 결합을 검증.
여기서는 분리된 헬퍼들 — control / status / insight / dispatch_sync 를 격리.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime

import pytest

from auto_trading.notify.commands import (
    CommandContext,
    InsightCooldown,
    dispatch_sync,
)
from auto_trading.notify.commands.control import (
    handle_pause,
    handle_resume,
    handle_stop,
)
from auto_trading.notify.commands.status import (
    handle_balance,
    handle_last_signal,
    handle_pnl,
    handle_positions,
    handle_status,
)
from auto_trading.notify.state import HELP_TEXT, RunnerStateSnapshot


def _state(**overrides: object) -> RunnerStateSnapshot:
    base: dict[str, object] = dict(
        mode="mock",
        dry_run=True,
        symbols=["005930"],
        strategies={"005930": "Sma"},
        started_at=datetime(2026, 5, 1, 9, 0),
        snapshot=None,
        account=None,
        recent_signals={},
        balance_failures=0,
    )
    base.update(overrides)
    return RunnerStateSnapshot(**base)  # type: ignore[arg-type]


# -- control --


def test_handle_pause_disabled_when_callback_none() -> None:
    msg = handle_pause(None)
    assert "비활성" in msg


def test_handle_pause_invokes_callback_and_returns_ack() -> None:
    calls: list[None] = []
    msg = handle_pause(lambda: calls.append(None))
    assert calls == [None]
    assert "일시정지" in msg
    assert "/resume" in msg


def test_handle_resume_and_stop_disabled_messages() -> None:
    assert "비활성" in handle_resume(None)
    assert "비활성" in handle_stop(None)


def test_handle_stop_invokes_callback_and_warns_irreversible() -> None:
    calls: list[None] = []
    msg = handle_stop(lambda: calls.append(None))
    assert calls == [None]
    assert "되돌릴 수 없" in msg


# -- status --


def test_handle_status_uses_format_status() -> None:
    msg = handle_status(_state())
    assert "러너 상태" in msg
    assert "005930" in msg


def test_handle_balance_no_snapshot_shows_pending_message() -> None:
    msg = handle_balance(_state(snapshot=None))
    assert "잔고 조회 전" in msg


def test_handle_positions_empty_returns_no_position_label() -> None:
    msg = handle_positions(_state(snapshot=None))
    assert "포지션 없음" in msg


def test_handle_pnl_returns_pnl_section_label() -> None:
    msg = handle_pnl(_state())
    assert "손익" in msg


def test_handle_last_signal_empty_returns_no_signal_message() -> None:
    msg = handle_last_signal(_state(recent_signals={}))
    assert "아직 시그널 없음" in msg


# -- dispatch_sync --


def _ctx(**overrides: object) -> CommandContext:
    base: dict[str, object] = dict(state_provider=_state)
    base.update(overrides)
    return CommandContext(**base)  # type: ignore[arg-type]


def test_dispatch_help_and_start_return_help_text() -> None:
    assert dispatch_sync("/help", _ctx()) == HELP_TEXT
    assert dispatch_sync("/start", _ctx()) == HELP_TEXT


def test_dispatch_unknown_command_falls_back_to_help() -> None:
    assert dispatch_sync("/unknown", _ctx()) == HELP_TEXT


def test_dispatch_status_routes_to_state_provider() -> None:
    msg = dispatch_sync("/status", _ctx())
    assert "러너 상태" in msg


def test_dispatch_pause_invokes_callback_through_ctx() -> None:
    calls: list[None] = []
    ctx = _ctx(on_pause=lambda: calls.append(None))
    msg = dispatch_sync("/pause", ctx)
    assert calls == [None]
    assert "일시정지" in msg


def test_dispatch_pause_disabled_when_ctx_callback_none() -> None:
    msg = dispatch_sync("/pause", _ctx(on_pause=None))
    assert "비활성" in msg


# -- InsightCooldown --


@pytest.mark.asyncio
async def test_insight_cooldown_returns_inactive_when_callback_none() -> None:
    msg = await InsightCooldown(None).handle()
    assert "비활성" in msg


@pytest.mark.asyncio
async def test_insight_cooldown_calls_callback_and_returns_text() -> None:
    async def _cb() -> str:
        return "🧭 ok"

    msg = await InsightCooldown(_cb).handle()
    assert msg == "🧭 ok"


@pytest.mark.asyncio
async def test_insight_cooldown_blocks_second_call_within_window() -> None:
    calls = 0

    async def _cb() -> str:
        nonlocal calls
        calls += 1
        return "ok"

    cd = InsightCooldown(_cb, cooldown_sec=300)
    await cd.handle()
    second = await cd.handle()
    assert "쿨다운" in second
    assert calls == 1


@pytest.mark.asyncio
async def test_insight_cooldown_resets_after_window() -> None:
    """clock 주입으로 결정론 검증 — 실시간 sleep 없이."""
    times = iter(
        [
            datetime(2026, 5, 9, 12, 0, 0),
            datetime(2026, 5, 9, 12, 5, 1),  # 301초 뒤 → 윈도우 통과
        ]
    )

    async def _cb() -> str:
        return "ok"

    cd = InsightCooldown(_cb, cooldown_sec=300, clock=lambda: next(times))
    await cd.handle()
    second = await cd.handle()
    assert second == "ok"


@pytest.mark.asyncio
async def test_insight_cooldown_swallows_callback_exception() -> None:
    """LLM 실패가 dispatch 루프를 죽이지 않게 — 안내 문자열로 변환."""

    async def _cb() -> str:
        raise RuntimeError("boom")

    msg = await InsightCooldown(_cb).handle()
    assert "실패" in msg


@pytest.mark.asyncio
async def test_insight_cooldown_advances_last_at_even_on_failure() -> None:
    """실패해도 cooldown 갱신 — 즉시 재시도가 또 비용 호출 가지 않게."""
    calls = 0

    async def _cb() -> str:
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    cd = InsightCooldown(_cb, cooldown_sec=300)
    await cd.handle()  # 첫 호출 — 실패하지만 last_at 갱신
    # 곧바로 두 번째 — 쿨다운 메시지 받아야 (실제 _cb 다시 안 부르고)
    second = await cd.handle()
    assert "쿨다운" in second
    assert calls == 1


# -- 회귀 보호: pytest-asyncio mode auto 설정에 의존하지 않는 sanity --


def test_command_context_is_frozen_dataclass() -> None:
    """CommandContext 가 immutable — 우연한 ctx 변형 회귀 방지."""
    ctx = _ctx()
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.on_pause = lambda: None  # type: ignore[misc]
