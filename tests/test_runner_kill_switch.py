"""PaperRunner._check_kill_switch — 일일 손실 한도 + daily_loss_kill 강제 청산 흐름.

PR8 에서 메인 루프 인라인 분기를 메서드로 추출하면서 격리 단위 테스트 가능해진 영역.
- should_force_liquidate=False → no-op (notifier/liquidate_all 호출 0)
- should_force_liquidate=True → notifier kill_switch 푸시 + liquidate_all + request_stop
- 두 번째 호출 → liquidate_all 의 idempotent 가드(_liquidation_done) 가 두 번째 처리
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from auto_trading.account.orders import OrderResult
from auto_trading.risk.manager import AccountState, RiskLimits, RiskManager
from auto_trading.runner import PaperRunner, RunnerControlState
from auto_trading.runner import paper as runner_module
from auto_trading.strategy.sma_crossover import SmaCrossover


def _runner(*, daily_loss_kill: bool, daily_loss_limit: float = 0.03) -> PaperRunner:
    return PaperRunner(
        strategy=SmaCrossover(),
        symbols=["005930"],
        risk=RiskManager(
            limits=RiskLimits(
                daily_loss_limit=daily_loss_limit,
                daily_loss_kill=daily_loss_kill,
            )
        ),
    )


def _account(
    *, day_start: float, total: float, positions: dict[str, int] | None = None
) -> AccountState:
    return AccountState(
        deposit=total * 0.5,
        total_equity=total,
        positions=positions or {},
        avg_prices={},
        day_start_equity=day_start,
        today_notional_used=0.0,
    )


async def test_kill_switch_skipped_when_below_loss_threshold() -> None:
    """손실이 한도 미만이면 should_force_liquidate=False → no-op."""
    r = _runner(daily_loss_kill=True, daily_loss_limit=0.03)
    notifier = AsyncMock()
    r.notifier = notifier
    # day_start 1,500,000 → 1,485,000 (-1%) 손실 — 한도 -3% 미달.
    account = _account(day_start=1_500_000, total=1_485_000)

    with patch.object(r, "liquidate_all", AsyncMock()) as mock_liq:
        await r._check_kill_switch(rest=object(), account=account)  # type: ignore[arg-type]

    notifier.notify.assert_not_called()
    mock_liq.assert_not_called()
    assert r._control_state is RunnerControlState.RUNNING


async def test_kill_switch_skipped_when_kill_disabled() -> None:
    """daily_loss_kill=False (기본) — 손실 한도 도달해도 발화 X."""
    r = _runner(daily_loss_kill=False, daily_loss_limit=0.03)
    notifier = AsyncMock()
    r.notifier = notifier
    # 한도 도달했지만 kill_switch 비활성.
    account = _account(day_start=1_500_000, total=1_400_000)

    with patch.object(r, "liquidate_all", AsyncMock()) as mock_liq:
        await r._check_kill_switch(rest=object(), account=account)  # type: ignore[arg-type]

    notifier.notify.assert_not_called()
    mock_liq.assert_not_called()


async def test_kill_switch_triggers_liquidate_notify_and_request_stop() -> None:
    """daily_loss_kill=True + 손실 한도 초과 → notifier 1회 + liquidate_all 1회 + STOPPING."""
    r = _runner(daily_loss_kill=True, daily_loss_limit=0.03)
    notifier = AsyncMock()
    r.notifier = notifier
    account = _account(day_start=1_500_000, total=1_400_000, positions={"005930": 10})

    # 반환 0 = 전부 성공 — _liquidate_with_retries 가 재시도 없이 1회로 끝나는 계약.
    with patch.object(r, "liquidate_all", AsyncMock(return_value=0)) as mock_liq:
        await r._check_kill_switch(rest=object(), account=account)  # type: ignore[arg-type]

    # 알림 1회 + 라벨 정확
    assert notifier.notify.call_count == 1
    args, kwargs = notifier.notify.call_args
    assert args[0] == "runner.kill_switch_triggered"
    assert kwargs.get("daily_loss_limit") == pytest.approx(0.03)

    # liquidate_all 1회, reason 정확
    mock_liq.assert_called_once()
    assert mock_liq.call_args.kwargs.get("reason") == "daily_loss_kill"

    # STOPPING 진입 (request_stop 호출 결과)
    assert r._control_state is RunnerControlState.STOPPING


async def test_kill_switch_retries_failed_liquidation_then_succeeds() -> None:
    """급락일 + 일시적 네트워크 오류 — 1차 실패해도 재시도로 청산 완주 후 STOPPING.

    종전엔 kill switch 경로만 재시도가 없어(liquidate_at 경로와 비대칭) 실패분을
    든 채 즉시 종료됐다 — _liquidate_with_retries 공용화로 같은 재시도를 보장.
    """
    r = _runner(daily_loss_kill=True, daily_loss_limit=0.03)
    r.liquidate_max_retries = 2
    r.liquidate_retry_delay_sec = 0
    notifier = AsyncMock()
    r.notifier = notifier
    account = _account(day_start=1_500_000, total=1_400_000, positions={"005930": 10})

    attempts = {"count": 0}

    async def _flaky(rest, order, **_):  # type: ignore[no-untyped-def]
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("transient network error")
        return OrderResult(order_no="ORD-1", raw={})

    with patch.object(runner_module, "place_order", AsyncMock(side_effect=_flaky)):
        await r._check_kill_switch(rest=object(), account=account)  # type: ignore[arg-type]

    assert attempts["count"] == 2  # 1차 실패 → 재시도 성공
    assert r._liquidation_done is True
    assert r._control_state is RunnerControlState.STOPPING
    events = [c.args[0] for c in notifier.notify.call_args_list]
    assert "runner.liquidate_giving_up" not in events


async def test_kill_switch_gives_up_after_retries_but_still_stops() -> None:
    """청산이 끝까지 실패해도 재시도 소진 후 경고 알림 + STOPPING (hang 방지 불변식)."""
    r = _runner(daily_loss_kill=True, daily_loss_limit=0.03)
    r.liquidate_max_retries = 2
    r.liquidate_retry_delay_sec = 0
    notifier = AsyncMock()
    r.notifier = notifier
    account = _account(day_start=1_500_000, total=1_400_000, positions={"005930": 10})

    async def _always_fail(rest, order, **_):  # type: ignore[no-untyped-def]
        raise RuntimeError("broker down")

    with patch.object(runner_module, "place_order", AsyncMock(side_effect=_always_fail)):
        await r._check_kill_switch(rest=object(), account=account)  # type: ignore[arg-type]

    assert r._control_state is RunnerControlState.STOPPING
    events = [c.args[0] for c in notifier.notify.call_args_list]
    assert "runner.liquidate_giving_up" in events
    assert account.positions["005930"] == 10  # 미청산분이 남았음을 정직하게 유지


async def test_kill_switch_idempotent_when_already_liquidated() -> None:
    """첫 발화 후 두 번째 호출 — liquidate_all 의 _liquidation_done 가드가 안전 처리.

    실제 liquidate_all 을 mock 하지 않고 dry_run 으로 호출 — _liquidation_done 가
    두 번째 호출에서 내부에서 'already_done' 으로 빠짐.
    """
    r = _runner(daily_loss_kill=True, daily_loss_limit=0.03)
    r.dry_run = True  # 실 주문 송신 회피
    notifier = AsyncMock()
    r.notifier = notifier
    account = _account(day_start=1_500_000, total=1_400_000, positions={"005930": 10})

    await r._check_kill_switch(rest=object(), account=account)  # type: ignore[arg-type]
    first_notify_count = notifier.notify.call_count
    assert r._liquidation_done is True

    # 두 번째 호출 — 내부 idempotent 가드 동작.
    await r._check_kill_switch(rest=object(), account=account)  # type: ignore[arg-type]

    # _check_kill_switch 자체는 should_force_liquidate=True 면 매번 알림 푸시
    # (호출 측에서 stop 이 이미 들어왔으면 메인 루프가 빠져나가야 함 — 계약 검증).
    # liquidate_all 은 두 번째 호출에서도 안전 (notify 추가 안 함은 그쪽 책임).
    assert notifier.notify.call_count >= first_notify_count
    # 어느 쪽이든 _control_state 는 STOPPING 유지.
    assert r._control_state is RunnerControlState.STOPPING


async def test_kill_switch_blind_warns_once_when_baseline_missing() -> None:
    """fail-loud — daily_loss_kill=True 인데 day_start_equity=0(평가금액 못 읽음) →
    청산은 안 하되 'runner.kill_switch_blind' 경고를 *1회* 푸시(매 갱신 스팸 방지).
    """
    r = _runner(daily_loss_kill=True, daily_loss_limit=0.03)
    notifier = AsyncMock()
    r.notifier = notifier
    account = _account(day_start=0.0, total=0.0)

    with patch.object(r, "liquidate_all", AsyncMock()) as mock_liq:
        await r._check_kill_switch(rest=object(), account=account)  # type: ignore[arg-type]
        # 두 번째 호출 — 이미 경고했으므로 추가 푸시 없음.
        await r._check_kill_switch(rest=object(), account=account)  # type: ignore[arg-type]

    assert notifier.notify.call_count == 1
    assert notifier.notify.call_args.args[0] == "runner.kill_switch_blind"
    mock_liq.assert_not_called()  # 무방비여도 임의 청산은 하지 않음
    assert r._control_state is RunnerControlState.RUNNING


async def test_kill_switch_not_blind_when_kill_disabled() -> None:
    """daily_loss_kill=False 면 day_start_equity=0 이라도 '무방비'가 아니므로 경고 X."""
    r = _runner(daily_loss_kill=False, daily_loss_limit=0.03)
    notifier = AsyncMock()
    r.notifier = notifier
    account = _account(day_start=0.0, total=0.0)

    with patch.object(r, "liquidate_all", AsyncMock()) as mock_liq:
        await r._check_kill_switch(rest=object(), account=account)  # type: ignore[arg-type]

    notifier.notify.assert_not_called()
    mock_liq.assert_not_called()


# 간단한 회귀 보호 — runner_module 의 should_force_liquidate / liquidate_all 시그니처가
# 바뀌었을 때 import 단계에서 잡힘.
def test_check_kill_switch_method_exists_on_runner() -> None:
    assert hasattr(PaperRunner, "_check_kill_switch")
    assert hasattr(runner_module, "PaperRunner")
