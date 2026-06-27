"""DailyReport JSON I/O — `data/reports/daily-<date>.json` 저장·로드.

⚠️ 직렬화 footgun(reports/CLAUDE.md): 저장은 `asdict` 로 전 필드 자동이지만 복원
`_from_json_dict` 는 **수동**이라 DailyReport 에 필드를 더하면 양쪽을 함께 고쳐야 한다
(과거 `turnover_ratio` 복원 누락으로 항상 None 이던 전례).
`test_save_load_roundtrip_preserves_every_field` 가 회귀를 잡는다.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .models import (
    BlockedOutcome,
    ClosingHolding,
    DailyReport,
    LiquidationRecord,
    MarketGateInfo,
    RoundTrip,
    TimelineEvent,
    TradeRecord,
)


def save_daily_report(report: DailyReport, *, root: Path | str = "data/reports") -> Path:
    """`<root>/daily-<date>.json` 으로 저장. 디렉터리 자동 생성."""
    out_dir = Path(root)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"daily-{report.date}.json"
    payload = _to_json_dict(report)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


def load_daily_report(path: Path | str) -> DailyReport:
    """저장된 JSON → DailyReport 라운드트립."""
    raw = Path(path).read_text(encoding="utf-8")
    payload = json.loads(raw)
    return _from_json_dict(payload)


def _to_json_dict(report: DailyReport) -> dict[str, Any]:
    d = asdict(report)
    # tuple → list (JSON 호환)
    d["discovery_blocked"] = [list(t) for t in report.discovery_blocked]
    return d


def _from_json_dict(payload: dict[str, Any]) -> DailyReport:
    chosen_names_raw = payload.get("discovery_chosen_names") or {}
    chosen_names = {str(k): str(v) for k, v in chosen_names_raw.items() if isinstance(k, str)}
    closing_holdings_raw = payload.get("closing_holdings") or []
    closing_holdings = [ClosingHolding(**h) for h in closing_holdings_raw]
    timeline_raw = payload.get("timeline") or []
    timeline = [TimelineEvent(**ev) for ev in timeline_raw if isinstance(ev, dict)]
    round_trips_raw = payload.get("round_trips") or []
    round_trips = [RoundTrip(**rt) for rt in round_trips_raw if isinstance(rt, dict)]
    blocked_outcomes_raw = payload.get("blocked_outcomes") or []
    blocked_outcomes = [BlockedOutcome(**b) for b in blocked_outcomes_raw if isinstance(b, dict)]
    return DailyReport(
        date=payload["date"],
        mode=payload["mode"],
        dry_run=payload["dry_run"],
        discovery_chosen=list(payload.get("discovery_chosen") or []),
        discovery_blocked=[
            (str(t[0]), str(t[1]))
            for t in (payload.get("discovery_blocked") or [])
            if isinstance(t, list | tuple) and len(t) >= 2
        ],
        market_gates=[MarketGateInfo(**g) for g in payload.get("market_gates") or []],
        buys=[TradeRecord(**t) for t in payload.get("buys") or []],
        liquidations=[LiquidationRecord(**li) for li in payload.get("liquidations") or []],
        error_count=int(payload.get("error_count") or 0),
        note_ko=str(payload.get("note_ko") or ""),
        realized_pnl=payload.get("realized_pnl"),
        realized_pnl_pct=payload.get("realized_pnl_pct"),
        sells=[TradeRecord(**t) for t in payload.get("sells") or []],
        excluded_etf_count=int(payload.get("excluded_etf_count") or 0),
        rank_candidates=list(payload.get("rank_candidates") or []),
        discovery_chosen_names=chosen_names,
        closing_cash=payload.get("closing_cash"),
        closing_equity=payload.get("closing_equity"),
        closing_total_pnl=payload.get("closing_total_pnl"),
        closing_holdings=closing_holdings,
        total_fees=payload.get("total_fees"),
        total_taxes=payload.get("total_taxes"),
        total_slippage=payload.get("total_slippage"),
        realized_pnl_after_cost=payload.get("realized_pnl_after_cost"),
        timeline=timeline,
        # 라운드트립 유실 복원(2026-06-10) — 저장은 asdict 로 전 필드가 나가는데
        # 복원에서 빠져 있어 turnover_ratio(과매매 진단 핵심)가 주간 인사이트·
        # 대시보드에서 항상 None 으로 떨어졌다.
        traded_notional=payload.get("traded_notional"),
        turnover_ratio=payload.get("turnover_ratio"),
        orders_failed_count=int(payload.get("orders_failed_count") or 0),
        liquidations_failed_count=int(payload.get("liquidations_failed_count") or 0),
        balance_failure_count=int(payload.get("balance_failure_count") or 0),
        fatal_count=int(payload.get("fatal_count") or 0),
        # 2026-06-15 신규 — 직렬화 footgun 회귀 방지: asdict 는 자동이지만 복원은 수동.
        regime=payload.get("regime"),
        breadth_advancing_pct=payload.get("breadth_advancing_pct"),
        breadth_mean_change_pct=payload.get("breadth_mean_change_pct"),
        breadth_passed=payload.get("breadth_passed"),
        round_trips=round_trips,
        blocked_outcomes=blocked_outcomes,
        no_trade_reason=payload.get("no_trade_reason"),
        today_notional_used=payload.get("today_notional_used"),
        reentry_blocked_count=int(payload.get("reentry_blocked_count") or 0),
    )
