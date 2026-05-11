"""auto_trading.preflight 회귀.

29단계 PR8 (#17) — 5개 활성 게이트의 통과/실패 케이스 + 미구현 placeholder.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from auto_trading._common import KST, Side
from auto_trading.account.order_journal import OrderJournal
from auto_trading.account.orders import Order
from auto_trading.llm.cost_tracker import CostTracker
from auto_trading.preflight import (
    GateResult,
    check_heartbeat_recent,
    check_kill_switch_simulation,
    check_latest_backtest_sharpe,
    check_llm_cost_within_budget,
    check_no_pending_orders,
    check_recent_paper_day_mdd,
    check_risk_limits_active,
    run_all_gates,
)

# ---- check_risk_limits_active -----------------------------------------------


def test_risk_limits_active_passes_with_stop_loss_and_kill(tmp_path: Path) -> None:
    profile = tmp_path / "p.yaml"
    profile.write_text(
        "risk:\n  stop_loss_pct: 0.01\n  daily_loss_kill: true\n",
        encoding="utf-8",
    )
    result = check_risk_limits_active(profile)
    assert result.ok is True
    assert "stop_loss_pct=0.01" in result.detail


def test_risk_limits_active_fails_when_kill_off(tmp_path: Path) -> None:
    profile = tmp_path / "p.yaml"
    profile.write_text(
        "risk:\n  stop_loss_pct: 0.01\n  daily_loss_kill: false\n",
        encoding="utf-8",
    )
    result = check_risk_limits_active(profile)
    assert result.ok is False
    assert "daily_loss_kill=False" in result.detail


def test_risk_limits_active_fails_when_stop_loss_zero(tmp_path: Path) -> None:
    profile = tmp_path / "p.yaml"
    profile.write_text(
        "risk:\n  stop_loss_pct: 0\n  daily_loss_kill: true\n",
        encoding="utf-8",
    )
    result = check_risk_limits_active(profile)
    assert result.ok is False
    assert "stop_loss_pct=0" in result.detail


def test_risk_limits_active_fails_when_profile_missing(tmp_path: Path) -> None:
    result = check_risk_limits_active(tmp_path / "nope.yaml")
    assert result.ok is False
    assert "없음" in result.detail


def test_risk_limits_active_fails_on_invalid_yaml(tmp_path: Path) -> None:
    profile = tmp_path / "bad.yaml"
    profile.write_text(": : : not yaml", encoding="utf-8")
    result = check_risk_limits_active(profile)
    assert result.ok is False


# ---- check_no_pending_orders -------------------------------------------------


def test_no_pending_orders_passes_when_journal_missing(tmp_path: Path) -> None:
    result = check_no_pending_orders(tmp_path / "no.jsonl")
    assert result.ok is True
    assert "실행 이력 0" in result.detail


def test_no_pending_orders_passes_when_all_resolved(tmp_path: Path) -> None:
    journal = OrderJournal(path=tmp_path / "state.jsonl")
    order = Order(symbol="005930", side=Side.BUY, quantity=1)
    journal.submit(client_id="c1", order=order)
    journal.accepted(client_id="c1", order_no="OK")
    result = check_no_pending_orders(journal.path)
    assert result.ok is True


def test_no_pending_orders_fails_with_unresolved_submit(tmp_path: Path) -> None:
    journal = OrderJournal(path=tmp_path / "state.jsonl")
    order = Order(symbol="005930", side=Side.BUY, quantity=1)
    journal.submit(client_id="c1", order=order)
    # accepted/failed 없음.
    result = check_no_pending_orders(journal.path)
    assert result.ok is False
    assert "1건" in result.detail


# ---- check_heartbeat_recent --------------------------------------------------


def test_heartbeat_recent_passes_for_fresh_file(tmp_path: Path) -> None:
    path = tmp_path / "hb.txt"
    path.write_text("now", encoding="utf-8")
    result = check_heartbeat_recent(path, max_age_sec=3600)
    assert result.ok is True


def test_heartbeat_recent_fails_when_missing(tmp_path: Path) -> None:
    result = check_heartbeat_recent(tmp_path / "nope.txt")
    assert result.ok is False
    assert "없음" in result.detail


def test_heartbeat_recent_fails_when_stale(tmp_path: Path) -> None:
    path = tmp_path / "hb.txt"
    path.write_text("old", encoding="utf-8")
    old = path.stat().st_mtime - 7200
    os.utime(path, (old, old))
    result = check_heartbeat_recent(path, max_age_sec=3600)
    assert result.ok is False
    assert "갱신 누락" in result.detail


# ---- check_recent_paper_day_mdd ----------------------------------------------


def _write_daily(reports: Path, date: str, pnl_pct: float) -> None:
    reports.mkdir(parents=True, exist_ok=True)
    (reports / f"daily-{date}.json").write_text(
        json.dumps({"date": date, "realized_pnl_pct": pnl_pct}),
        encoding="utf-8",
    )


def test_recent_paper_day_mdd_passes_when_worst_above_threshold(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    _write_daily(reports, "2026-05-01", -1.5)  # -1.5%
    _write_daily(reports, "2026-05-02", 0.8)
    _write_daily(reports, "2026-05-03", -0.3)
    result = check_recent_paper_day_mdd(reports, days=30, threshold=-0.05)
    assert result.ok is True
    assert "-1.50%" in result.detail or "-1.5" in result.detail


def test_recent_paper_day_mdd_fails_when_worst_below_threshold(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    _write_daily(reports, "2026-05-01", -7.0)  # -7%
    _write_daily(reports, "2026-05-02", 0.5)
    result = check_recent_paper_day_mdd(reports, days=30, threshold=-0.05)
    assert result.ok is False


def test_recent_paper_day_mdd_fails_when_no_reports(tmp_path: Path) -> None:
    result = check_recent_paper_day_mdd(tmp_path / "empty", days=30)
    assert result.ok is False


def test_recent_paper_day_mdd_fails_when_no_pnl_field(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "daily-2026-05-01.json").write_text(
        json.dumps({"date": "2026-05-01"}),  # realized_pnl_pct 없음
        encoding="utf-8",
    )
    result = check_recent_paper_day_mdd(reports, days=30)
    assert result.ok is False


# ---- check_latest_backtest_sharpe --------------------------------------------


def _write_backtest(root: Path, name: str, sharpe: float) -> Path:
    run = root / name
    run.mkdir(parents=True, exist_ok=True)
    (run / "report.json").write_text(json.dumps({"sharpe": sharpe}), encoding="utf-8")
    return run


def test_latest_backtest_sharpe_passes_when_above_threshold(tmp_path: Path) -> None:
    _write_backtest(tmp_path, "run-1", 0.85)
    result = check_latest_backtest_sharpe(tmp_path, threshold=0.0)
    assert result.ok is True
    assert "0.850" in result.detail


def test_latest_backtest_sharpe_fails_when_below_threshold(tmp_path: Path) -> None:
    _write_backtest(tmp_path, "run-1", -0.2)
    result = check_latest_backtest_sharpe(tmp_path, threshold=0.0)
    assert result.ok is False


def test_latest_backtest_sharpe_picks_most_recent_run(tmp_path: Path) -> None:
    """여러 폴더 중 mtime 가장 최근."""
    old = _write_backtest(tmp_path, "run-old", -1.0)
    # mtime 인위적 옛날.
    old_time = old.stat().st_mtime - 86400
    os.utime(old, (old_time, old_time))
    _write_backtest(tmp_path, "run-new", 0.5)
    result = check_latest_backtest_sharpe(tmp_path, threshold=0.0)
    assert result.ok is True
    assert "run-new" in result.detail


def test_latest_backtest_sharpe_fails_when_no_runs(tmp_path: Path) -> None:
    result = check_latest_backtest_sharpe(tmp_path)
    assert result.ok is False


# ---- check_kill_switch_simulation (29단계 PR26) ------------------------------


def _write_kill_profile(
    path: Path, *, daily_loss_kill: bool = True, daily_loss_limit: float = 0.03
) -> None:
    path.write_text(
        f"risk:\n  stop_loss_pct: 0.01\n  daily_loss_kill: {str(daily_loss_kill).lower()}\n"
        f"  daily_loss_limit: {daily_loss_limit}\n",
        encoding="utf-8",
    )


def test_kill_switch_simulation_passes_with_daily_loss_kill(tmp_path: Path) -> None:
    profile = tmp_path / "p.yaml"
    _write_kill_profile(profile, daily_loss_kill=True, daily_loss_limit=0.03)
    result = check_kill_switch_simulation(profile)
    assert result.ok is True
    assert "3.0%" in result.detail or "-3.0%" in result.detail


def test_kill_switch_simulation_fails_when_kill_off(tmp_path: Path) -> None:
    profile = tmp_path / "p.yaml"
    _write_kill_profile(profile, daily_loss_kill=False)
    result = check_kill_switch_simulation(profile)
    assert result.ok is False
    assert "daily_loss_kill" in result.detail


def test_kill_switch_simulation_fails_when_profile_missing(tmp_path: Path) -> None:
    result = check_kill_switch_simulation(tmp_path / "nope.yaml")
    assert result.ok is False
    assert "없음" in result.detail


def test_kill_switch_simulation_fails_on_invalid_yaml(tmp_path: Path) -> None:
    profile = tmp_path / "bad.yaml"
    profile.write_text(": : : not yaml", encoding="utf-8")
    result = check_kill_switch_simulation(profile)
    assert result.ok is False


def test_kill_switch_simulation_fails_when_limit_zero(tmp_path: Path) -> None:
    """daily_loss_limit=0 이면 시뮬 의미 없음 → fail."""
    profile = tmp_path / "p.yaml"
    _write_kill_profile(profile, daily_loss_kill=True, daily_loss_limit=0.0)
    result = check_kill_switch_simulation(profile)
    assert result.ok is False
    assert "daily_loss_limit" in result.detail


# ---- check_llm_cost_within_budget (29단계 PR15) -----------------------------


def test_llm_cost_skipped_when_path_none() -> None:
    """cost 추적 비활성 환경 → skipped 로 표시 (활성 게이트 X)."""
    result = check_llm_cost_within_budget(None)
    assert result.skipped is True
    assert "비활성" in result.detail


def test_llm_cost_passes_when_under_budget(tmp_path: Path) -> None:
    path = tmp_path / "cost.jsonl"
    tracker = CostTracker(path=path, monthly_budget_usd=5.0)
    tracker.record(
        model="claude-opus-4-7",
        input_tokens=1_000,
        output_tokens=500,
        ts=datetime.now(tz=KST),
    )
    result = check_llm_cost_within_budget(path, monthly_budget_usd=5.0)
    assert result.ok is True
    assert result.skipped is False
    assert "$5.00" in result.detail


def test_llm_cost_fails_when_over_budget(tmp_path: Path) -> None:
    path = tmp_path / "cost.jsonl"
    tracker = CostTracker(path=path, monthly_budget_usd=0.5)
    tracker.record(
        model="claude-opus-4-7",
        input_tokens=10_000,
        output_tokens=10_000,
        ts=datetime.now(tz=KST),
    )
    result = check_llm_cost_within_budget(path, monthly_budget_usd=0.5)
    assert result.ok is False
    assert result.skipped is False
    assert "한도 초과" in result.detail


def test_llm_cost_passes_with_alert_warning(tmp_path: Path) -> None:
    """알림 임계(80%) 도달했지만 한도 초과는 아닐 때 — ok=True 단 detail 에 경고."""
    path = tmp_path / "cost.jsonl"
    tracker = CostTracker(path=path, monthly_budget_usd=1.0)
    # $0.9 사용 — 90% (alert ≥80%, over <100%).
    tracker.record(
        model="claude-opus-4-7",
        input_tokens=10_000,
        output_tokens=10_000,
        ts=datetime.now(tz=KST),
    )
    result = check_llm_cost_within_budget(path, monthly_budget_usd=1.0)
    assert result.ok is True  # 100% 미만은 통과
    assert "알림 임계" in result.detail


# ---- run_all_gates -----------------------------------------------------------


def test_run_all_gates_returns_active_plus_skipped(tmp_path: Path) -> None:
    """6 활성 + 1 conditional(llm_cost — None 경로 → skipped) + 1 placeholder = 8건.
    skipped 는 2건 (1 placeholder + llm_cost 비활성).
    PR26 (#17 후속) — kill_switch_simulation 이 placeholder 에서 활성으로 승격."""
    results = run_all_gates(
        profile_path=tmp_path / "p.yaml",
        journal_path=tmp_path / "state.jsonl",
        heartbeat_path=tmp_path / "hb.txt",
        reports_root=tmp_path / "reports",
        backtests_root=tmp_path / "backtests",
    )
    assert len(results) == 8
    skipped = [r for r in results if r.skipped]
    assert len(skipped) == 2
    active = [r for r in results if not r.skipped]
    assert all(isinstance(r, GateResult) for r in active)
    # journal 미존재는 "실행 이력 0" 으로 정상 (no_pending_orders 통과). 나머지 5 활성은 실패.
    failed_active = [r for r in active if not r.ok]
    assert len(failed_active) == 5
    passed_active_names = {r.name for r in active if r.ok}
    assert passed_active_names == {"no_pending_orders"}
