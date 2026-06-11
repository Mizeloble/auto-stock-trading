"""일일 한도 기준 영속(day_state) — 비정상 재시동 시 한도 희석 방지 (2026-06-10 검토).

검증:
- DayStateStore: 저장/복원 라운드트립, 다른 날짜·손상 파일이면 None.
- 러너 첫 스냅샷: 같은 날짜 저장분이 있으면 day_start_equity·매수대금·BUY 카운트·
  churn 누적을 복원 (재시작 시점 평가액으로 baseline 이 리셋되지 않음).
- 잔고 갱신 시 자동 저장.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import auto_trading.runner as runner_module
from auto_trading._common import KST
from auto_trading.account.balance import AccountSnapshot
from auto_trading.runner import PaperRunner
from auto_trading.runner.day_state import DayState, DayStateStore
from auto_trading.strategy.sma_crossover import SmaCrossover


def _today() -> date:
    return datetime.now(KST).date()


def _store(tmp_path: Path) -> DayStateStore:
    return DayStateStore(path=tmp_path / "day-state.json")


def _snapshot(deposit: int) -> AccountSnapshot:
    return AccountSnapshot(
        deposit=Decimal(deposit),
        total_evaluation=Decimal("0"),
        total_pnl=Decimal("0"),
        holdings=[],
    )


# ---- DayStateStore 단위 --------------------------------------------------------


def test_store_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    state = DayState(
        date=_today().isoformat(),
        day_start_equity=10_000_000.0,
        today_notional_used=3_000_000.0,
        daily_buy_counts={"005930": 2},
        daily_realized_net=-120_000.0,
    )
    store.save(state)
    loaded = store.load(_today())
    assert loaded == state


def test_store_ignores_other_date(tmp_path: Path) -> None:
    """어제 저장분은 복원하지 않음 — 자정 리셋 의미 보존."""
    store = _store(tmp_path)
    yesterday = (_today() - timedelta(days=1)).isoformat()
    store.save(DayState(date=yesterday, day_start_equity=10_000_000.0))
    assert store.load(_today()) is None


def test_store_survives_corrupt_file(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{broken json", encoding="utf-8")
    assert store.load(_today()) is None


def test_store_missing_file_returns_none(tmp_path: Path) -> None:
    assert _store(tmp_path).load(_today()) is None


# ---- 러너 통합 — 재시동 복원 ----------------------------------------------------


async def test_first_snapshot_restores_same_day_baseline(tmp_path: Path) -> None:
    """장중 재시동: baseline 이 재시작 시점 평가액(9.7M)이 아니라 저장분(10M)으로 복원.

    종전엔 -3% 한도가 9.7M 기준으로 다시 잡혀 그날 추가 -3% 를 더 허용했다.
    BUY 카운트·churn 누적도 함께 복원돼 회전율 가드·브레이크가 이어진다.
    """
    store = _store(tmp_path)
    store.save(
        DayState(
            date=_today().isoformat(),
            day_start_equity=10_000_000.0,
            today_notional_used=4_000_000.0,
            daily_buy_counts={"005930": 2},
            daily_realized_net=-150_000.0,
        )
    )
    r = PaperRunner(strategy=SmaCrossover(), symbols=["005930"], day_state=store)

    with patch.object(runner_module, "get_balance", AsyncMock(return_value=_snapshot(9_700_000))):
        state = await r._snapshot_account(rest=object(), previous=None)  # type: ignore[arg-type]

    assert state.day_start_equity == 10_000_000.0  # 재시작 시점 9.7M 아님
    assert state.today_notional_used == 4_000_000.0
    assert r._daily_buy_counts == {"005930": 2}
    assert r._daily_realized_net == -150_000.0


async def test_first_snapshot_without_saved_state_uses_current_equity(tmp_path: Path) -> None:
    """저장분이 없으면(정상 아침 시동) 기존 동작 — 현재 평가액이 baseline."""
    r = PaperRunner(strategy=SmaCrossover(), symbols=["005930"], day_state=_store(tmp_path))

    with patch.object(runner_module, "get_balance", AsyncMock(return_value=_snapshot(10_000_000))):
        state = await r._snapshot_account(rest=object(), previous=None)  # type: ignore[arg-type]

    assert state.day_start_equity == 10_000_000.0
    assert state.today_notional_used == 0.0


async def test_snapshot_persists_state_to_disk(tmp_path: Path) -> None:
    """잔고 갱신 때마다 디스크 저장 — 다음 재시동이 읽을 수 있게."""
    store = _store(tmp_path)
    r = PaperRunner(strategy=SmaCrossover(), symbols=["005930"], day_state=store)
    r._daily_realized_net = -50_000.0
    r._daily_buy_counts = {"005930": 1}
    r._daily_buy_counts_date = _today()

    with patch.object(runner_module, "get_balance", AsyncMock(return_value=_snapshot(10_000_000))):
        await r._snapshot_account(rest=object(), previous=None)  # type: ignore[arg-type]

    saved = store.load(_today())
    assert saved is not None
    assert saved.day_start_equity == 10_000_000.0
    assert saved.daily_buy_counts == {"005930": 1}
    assert saved.daily_realized_net == -50_000.0


async def test_runner_without_day_state_unchanged(tmp_path: Path) -> None:
    """day_state 미주입(테스트·기존 경로) — 종전 동작 그대로, 파일도 안 만듦."""
    r = PaperRunner(strategy=SmaCrossover(), symbols=["005930"])
    with patch.object(runner_module, "get_balance", AsyncMock(return_value=_snapshot(9_700_000))):
        state = await r._snapshot_account(rest=object(), previous=None)  # type: ignore[arg-type]
    assert state.day_start_equity == 9_700_000.0
    assert not (tmp_path / "day-state.json").exists()
