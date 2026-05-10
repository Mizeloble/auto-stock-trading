"""llm.cost_tracker 회귀.

29단계 PR13 (#16 1차).

검증:
- 모델별 가격이 정확하게 USD 로 환산.
- 알 수 없는 모델은 default(Opus) 가격 — 과대 추정.
- record() 가 JSONL 한 줄 append + CostEntry 반환.
- replay() 가 기록 순서대로 yield, 손상 라인 스킵.
- monthly_summary() 가 KST 월 기준으로만 집계.
- budget_status() alert/over 임계 분기.
- 기록 디스크 실패는 swallow (예외 X).
- 이번 달 / 다른 달 분리.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from auto_trading._common import KST
from auto_trading.llm.cost_tracker import (
    DEFAULT_BUDGET_USD,
    MODEL_PRICES_USD_PER_M,
    CostTracker,
    estimate_cost_usd,
)

# ---- estimate_cost_usd -------------------------------------------------------


def test_opus_price_calculation() -> None:
    # 1M input + 1M output = $15 + $75 = $90.
    assert estimate_cost_usd("claude-opus-4-7", 1_000_000, 1_000_000) == 90.0


def test_sonnet_price_calculation() -> None:
    # Sonnet: input $3, output $15 per 1M.
    cost = estimate_cost_usd("claude-sonnet-4-6", 100_000, 50_000)
    assert abs(cost - (100_000 * 3 + 50_000 * 15) / 1_000_000) < 1e-9


def test_haiku_price_calculation() -> None:
    cost = estimate_cost_usd("claude-haiku-4-5", 1_000, 500)
    assert abs(cost - (1_000 * 1 + 500 * 5) / 1_000_000) < 1e-9


def test_unknown_model_uses_default_opus_price() -> None:
    """알 수 없는 모델 → 가장 비싼 가격으로 과대 추정 (한도 보호)."""
    unknown_cost = estimate_cost_usd("claude-future-99", 1_000, 1_000)
    opus_cost = estimate_cost_usd("claude-opus-4-7", 1_000, 1_000)
    assert unknown_cost == opus_cost


def test_zero_tokens_produces_zero_cost() -> None:
    assert estimate_cost_usd("claude-opus-4-7", 0, 0) == 0.0


def test_model_prices_table_has_known_entries() -> None:
    """가격 표 변경 시 깨질 회귀 — 가격이 의도치 않게 0 이거나 음수가 되지 않게."""
    for model, (in_p, out_p) in MODEL_PRICES_USD_PER_M.items():
        assert in_p > 0, model
        assert out_p > 0, model


# ---- CostTracker.record + replay --------------------------------------------


def test_record_appends_jsonl_line(tmp_path: Path) -> None:
    tracker = CostTracker(path=tmp_path / "cost.jsonl")
    entry = tracker.record(model="claude-opus-4-7", input_tokens=1_000, output_tokens=500)

    lines = (tmp_path / "cost.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert "claude-opus-4-7" in lines[0]
    assert entry.model == "claude-opus-4-7"
    assert entry.input_tokens == 1_000
    assert entry.output_tokens == 500
    assert entry.cost_usd > 0


def test_replay_yields_entries_in_order(tmp_path: Path) -> None:
    tracker = CostTracker(path=tmp_path / "cost.jsonl")
    tracker.record(model="claude-opus-4-7", input_tokens=100, output_tokens=50)
    tracker.record(model="claude-sonnet-4-6", input_tokens=200, output_tokens=100)
    tracker.record(model="claude-haiku-4-5", input_tokens=300, output_tokens=150)

    models = [e.model for e in tracker.replay()]
    assert models == ["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5"]


def test_replay_skips_corrupt_line(tmp_path: Path) -> None:
    path = tmp_path / "cost.jsonl"
    path.write_text(
        '{"ts":"2026-05-10T10:00:00+09:00","model":"opus","input_tokens":1,"output_tokens":1,"cost_usd":0.01}\n'
        "이건 JSON 아님\n"
        '{"ts":"2026-05-10T11:00:00+09:00","model":"sonnet","input_tokens":2,"output_tokens":2,"cost_usd":0.02}\n',
        encoding="utf-8",
    )
    tracker = CostTracker(path=path)
    entries = list(tracker.replay())
    assert len(entries) == 2
    assert {e.model for e in entries} == {"opus", "sonnet"}


def test_replay_on_missing_file_yields_nothing(tmp_path: Path) -> None:
    tracker = CostTracker(path=tmp_path / "nope.jsonl")
    assert list(tracker.replay()) == []


def test_record_swallows_disk_failure(tmp_path: Path) -> None:
    """디스크 실패가 LLM 호출 성공을 막지 않게 swallow."""
    tracker = CostTracker(path=tmp_path / "cost.jsonl")
    with patch.object(Path, "open", side_effect=OSError("disk full")):
        # 예외 X — 정상 entry 반환.
        entry = tracker.record(model="claude-opus-4-7", input_tokens=10, output_tokens=10)
    assert entry.cost_usd > 0


# ---- monthly_summary --------------------------------------------------------


def test_monthly_summary_aggregates_only_target_month(tmp_path: Path) -> None:
    tracker = CostTracker(path=tmp_path / "cost.jsonl")
    may = datetime(2026, 5, 10, 10, tzinfo=KST)
    apr = datetime(2026, 4, 15, 10, tzinfo=KST)
    tracker.record(model="claude-opus-4-7", input_tokens=1_000, output_tokens=1_000, ts=may)
    tracker.record(model="claude-opus-4-7", input_tokens=2_000, output_tokens=2_000, ts=may)
    tracker.record(model="claude-opus-4-7", input_tokens=10_000, output_tokens=10_000, ts=apr)

    s_may = tracker.monthly_summary("2026-05")
    s_apr = tracker.monthly_summary("2026-04")
    assert s_may.call_count == 2
    assert s_apr.call_count == 1
    # 5월 합계 = 3000 입력 + 3000 출력 → (3000*15 + 3000*75) / 1M = 0.27
    assert abs(s_may.total_usd - 0.27) < 1e-6


def test_monthly_summary_groups_by_model(tmp_path: Path) -> None:
    tracker = CostTracker(path=tmp_path / "cost.jsonl")
    ts = datetime(2026, 5, 10, 10, tzinfo=KST)
    tracker.record(model="claude-opus-4-7", input_tokens=1_000, output_tokens=500, ts=ts)
    tracker.record(model="claude-sonnet-4-6", input_tokens=10_000, output_tokens=5_000, ts=ts)

    summary = tracker.monthly_summary("2026-05")
    assert "claude-opus-4-7" in summary.by_model
    assert "claude-sonnet-4-6" in summary.by_model
    # Sonnet 의 비용이 입력 10x + 출력 10x 더 많지만 단가가 5x 낮아 결과적으로 더 큼.
    assert summary.by_model["claude-sonnet-4-6"] > summary.by_model["claude-opus-4-7"]


def test_monthly_summary_empty_when_no_records(tmp_path: Path) -> None:
    tracker = CostTracker(path=tmp_path / "cost.jsonl")
    summary = tracker.monthly_summary("2026-05")
    assert summary.total_usd == 0.0
    assert summary.call_count == 0
    assert summary.by_model == {}


# ---- budget_status ----------------------------------------------------------


def test_budget_status_below_alert_threshold(tmp_path: Path) -> None:
    tracker = CostTracker(path=tmp_path / "cost.jsonl", monthly_budget_usd=5.0)
    ts = datetime(2026, 5, 10, 10, tzinfo=KST)
    # $1 정도만 사용 → 20% < 80%.
    tracker.record(model="claude-opus-4-7", input_tokens=20_000, output_tokens=10_000, ts=ts)
    status = tracker.budget_status("2026-05")
    assert status.alert is False
    assert status.over is False
    assert status.pct_used < 0.5


def test_budget_status_alert_at_80_percent(tmp_path: Path) -> None:
    tracker = CostTracker(path=tmp_path / "cost.jsonl", monthly_budget_usd=1.0, alert_pct=0.8)
    ts = datetime(2026, 5, 10, 10, tzinfo=KST)
    # $0.81 정도 — 80% 직후.
    # opus: input 1만 + output 1만 = (10k*15 + 10k*75) / 1M = $0.9
    tracker.record(model="claude-opus-4-7", input_tokens=10_000, output_tokens=10_000, ts=ts)
    status = tracker.budget_status("2026-05")
    assert status.alert is True
    assert status.over is False  # 90% — alert but not over


def test_budget_status_over_at_100_percent(tmp_path: Path) -> None:
    tracker = CostTracker(path=tmp_path / "cost.jsonl", monthly_budget_usd=0.5)
    ts = datetime(2026, 5, 10, 10, tzinfo=KST)
    # $0.9 사용 → 180% — over.
    tracker.record(model="claude-opus-4-7", input_tokens=10_000, output_tokens=10_000, ts=ts)
    status = tracker.budget_status("2026-05")
    assert status.alert is True
    assert status.over is True
    assert status.pct_used > 1.0


def test_budget_status_default_budget_is_5_dollars() -> None:
    """memory 의 cost discipline — 월 $5."""
    assert DEFAULT_BUDGET_USD == 5.0


def test_budget_status_handles_zero_spend(tmp_path: Path) -> None:
    tracker = CostTracker(path=tmp_path / "cost.jsonl", monthly_budget_usd=5.0)
    status = tracker.budget_status("2026-05")
    assert status.spent_usd == 0.0
    assert status.alert is False
    assert status.over is False
