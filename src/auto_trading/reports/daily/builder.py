"""DailyReport 빌더 — 로그·스냅샷 → 리포트 오케스트레이션.

빌더는 두 입력만 사용:
1. `LogEvents` (그날 로그 파일 파싱 결과)
2. `RunnerStateSnapshot` (러너의 인메모리 상태) — 선택적, 종료 직후만 가용

LLM 호출·외부 API 호출 없음. 결정론적 — 같은 입력이면 같은 출력.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ...notify.state import RunnerStateSnapshot
from ..log_parser import LogEvents
from ..snapshot_reader import parse_topcap_for_date
from ._util import _ts_to_kst_hhmm
from .aggregate import _aggregate_notional, _aggregate_realized, _estimate_costs
from .extract import (
    _build_blocked_outcomes,
    _derive_no_trade_reason,
    _enrich_buys,
    _enrich_round_trips,
    _extract_closing,
    _extract_discovery,
    _extract_gates,
    _extract_regime_breadth,
    _extract_trades,
    _is_buying_power_reject,
)
from .models import DailyReport
from .persist import load_daily_report, save_daily_report
from .render import _summary_ko
from .timeline import _build_timeline


def build_daily_report(
    *,
    date: str,
    events: LogEvents,
    snapshot: RunnerStateSnapshot | None,
    snapshot_root: Path | str = "data/snapshots",
) -> DailyReport:
    """LogEvents + 옵셔널 snapshot 으로 DailyReport 생성.

    무엇/왜:
    - 자동 호출(러너 종료 직후) 은 snapshot 이 있어 mode/dry_run 을 알 수 있음.
    - 수동 재생성(`auto-trade report --date …`) 은 snapshot=None — 로그만 보고 추정.
    - day-trade 가 아닌 일자(레거시 swing 또는 데이터 없음) 도 빈 DailyReport 로 안전하게 만듦.
    - snapshot_root: topcap 일중 스냅샷 폴더. 파일 없으면 진입 피처·MFE/MAE·차단
      기회비용은 모두 None/빈 리스트로 폴백(과거 일자 재생성 호환).
    """
    discovery_chosen, discovery_blocked = _extract_discovery(events)
    market_gates = _extract_gates(events)
    names = dict(events.symbol_names)
    buys, sells, liquidations = _extract_trades(events, names)
    orders_failed_count = len(events.orders_failed)
    liquidations_failed_count = len(events.liquidations_failed)
    # 매수여력 부족(일 매수대금 한도 소진 등)으로 막힌 재진입 신호 수 — "장 초반 쏠림" 진단용.
    reentry_blocked_count = sum(
        1 for ev in events.signal_rejected if _is_buying_power_reject(str(ev.get("reason") or ""))
    )
    # churn 브레이크(일일 실현손실 한도) — 발동 후 매수 신호마다 반복 기록되므로
    # 건수 + 최초 발동 시각만 집계. 로그는 시각순이라 [0] 이 최초 발동.
    daily_loss_blocked_count = len(events.daily_loss_blocks)
    daily_loss_blocked_first_time = (
        _ts_to_kst_hhmm(events.daily_loss_blocks[0].get("timestamp"))
        if events.daily_loss_blocks
        else None
    )
    fatal_count = len(events.fatal)
    balance_failure_count = 1 if events.balance_failures > 0 else 0
    error_count = (
        orders_failed_count + liquidations_failed_count + fatal_count + balance_failure_count
    )

    # snapshot 우선. 없으면 events 로 mode/dry_run 추정.
    if snapshot is not None:
        mode = snapshot.runner_mode
        dry_run = snapshot.dry_run
    else:
        mode = "day-trade" if (events.discovery is not None or events.rank is not None) else "swing"
        dry_run = bool(events.orders_simulated or events.liquidations_simulated)

    realized_pnl, realized_pnl_pct, round_trips = _aggregate_realized(buys, sells, liquidations)
    regime, breadth_adv, breadth_mean, breadth_passed = _extract_regime_breadth(events)
    # topcap 일중 스냅샷 조인 — 진입 피처·MFE/MAE·차단 기회비용. 파일 없으면 무영향.
    snaps = parse_topcap_for_date(date, root=snapshot_root)
    buys = _enrich_buys(buys, snaps)
    round_trips = _enrich_round_trips(round_trips, snaps)
    blocked_outcomes = _build_blocked_outcomes(discovery_blocked, names, snaps)
    no_trade_reason = _derive_no_trade_reason(
        events, buys, discovery_chosen, discovery_blocked, breadth_passed
    )
    closing_cash, closing_equity, closing_total_pnl, closing_holdings = _extract_closing(snapshot)
    # snapshot.holdings 의 name 도 흡수 — discovery 에 없던 종목(swing 등) 보강.
    if snapshot is not None and snapshot.snapshot is not None:
        for h in snapshot.snapshot.holdings:
            if h.name and h.symbol not in names:
                names[h.symbol] = h.name
    # chosen 뿐 아니라 blocked 후보 코드도 매핑 — 리포트 "차단:" 라인이 코드+이름으로
    # 보이게. daytrade.discovery 로그가 전 후보 names 를 남기므로 events 에 다 있음.
    discovery_syms = list(discovery_chosen) + [s for s, _ in discovery_blocked]
    chosen_names = {sym: names[sym] for sym in discovery_syms if sym in names}
    total_fees, total_taxes, total_slippage = _estimate_costs(buys, sells, liquidations)
    realized_pnl_after_cost = (
        realized_pnl - (total_fees + total_taxes + total_slippage)
        if realized_pnl is not None
        and total_fees is not None
        and total_taxes is not None
        and total_slippage is not None
        else None
    )

    traded_notional = _aggregate_notional(buys, sells, liquidations)
    day_start_equity = (
        float(snapshot.account.day_start_equity)
        if snapshot is not None
        and snapshot.account is not None
        and snapshot.account.day_start_equity > 0
        else None
    )
    turnover_ratio = (
        traded_notional / day_start_equity
        if traded_notional is not None and day_start_equity
        else None
    )
    # 그날 누적 매수대금 — 일 매수대금 한도 소진 여부를 사용자가 보게. snapshot 가용 시만.
    today_notional_used = (
        round(snapshot.account.today_notional_used)
        if snapshot is not None
        and snapshot.account is not None
        and snapshot.account.today_notional_used > 0
        else None
    )

    note_ko = _summary_ko(
        chosen=discovery_chosen,
        buys=buys,
        sells=sells,
        blocked=discovery_blocked,
        liquidations=liquidations,
        realized_pnl=realized_pnl,
        realized_pnl_after_cost=realized_pnl_after_cost,
        error_count=error_count,
        daily_loss_blocked_count=daily_loss_blocked_count,
        daily_loss_blocked_first_time=daily_loss_blocked_first_time,
    )

    timeline = _build_timeline(events, names)

    rank_candidates: list[str] = []
    excluded = 0
    if events.rank is not None:
        chosen = events.rank.get("chosen") or []
        # chosen 항목은 [symbol, market, volume] 또는 단순 symbol 리스트일 수 있음 — 양쪽 처리.
        for item in chosen:
            if isinstance(item, list | tuple) and item:
                rank_candidates.append(str(item[0]))
            elif isinstance(item, str):
                rank_candidates.append(item)
        excluded_raw = events.rank.get("excluded_count")
        if isinstance(excluded_raw, int):
            excluded = excluded_raw

    return DailyReport(
        date=date,
        mode=mode,
        dry_run=dry_run,
        discovery_chosen=discovery_chosen,
        discovery_blocked=discovery_blocked,
        market_gates=market_gates,
        buys=buys,
        liquidations=liquidations,
        error_count=error_count,
        note_ko=note_ko,
        realized_pnl=realized_pnl,
        realized_pnl_pct=realized_pnl_pct,
        sells=sells,
        excluded_etf_count=excluded,
        rank_candidates=rank_candidates,
        discovery_chosen_names=chosen_names,
        closing_cash=closing_cash,
        closing_equity=closing_equity,
        closing_total_pnl=closing_total_pnl,
        closing_holdings=closing_holdings,
        total_fees=total_fees,
        total_taxes=total_taxes,
        total_slippage=total_slippage,
        realized_pnl_after_cost=realized_pnl_after_cost,
        timeline=timeline,
        traded_notional=traded_notional,
        turnover_ratio=turnover_ratio,
        orders_failed_count=orders_failed_count,
        liquidations_failed_count=liquidations_failed_count,
        balance_failure_count=balance_failure_count,
        fatal_count=fatal_count,
        regime=regime,
        breadth_advancing_pct=breadth_adv,
        breadth_mean_change_pct=breadth_mean,
        breadth_passed=breadth_passed,
        round_trips=round_trips,
        blocked_outcomes=blocked_outcomes,
        no_trade_reason=no_trade_reason,
        today_notional_used=today_notional_used,
        reentry_blocked_count=reentry_blocked_count,
        daily_loss_blocked_count=daily_loss_blocked_count,
        daily_loss_blocked_first_time=daily_loss_blocked_first_time,
    )


def refresh_snapshot_joins(
    date: str,
    *,
    report_root: Path | str = "data/reports",
    snapshot_root: Path | str = "data/snapshots",
) -> Path | None:
    """저장된 daily-<date>.json 의 스냅샷 조인 필드만 다시 채워 저장.

    무엇/왜:
    - 게이트 차단일엔 러너가 개장 직후(~09:02) 종료하며 리포트를 저장하는데, 그
      시점엔 topcap 스냅샷이 1라운드뿐이라 차단 종목 성적(blocked_outcomes)·진입
      피처·MFE/MAE 조인이 비거나 개장가로 얼어붙는다. 스냅샷 원본은 15:30까지
      정상 축적되므로, 장 마감 후 이 함수로 조인 필드만 소급 갱신한다.
    - 전체 재빌드(build_daily_report)와 달리 기존 리포트의 나머지(closing 잔고·
      비용·타임라인 등)는 그대로 보존 — 러너 스냅샷 없이는 복원 불가능한 필드를
      잃지 않는다.
    - 병합 정책: 새 조인 값이 None 이면 기존 값을 유지(스냅샷 결손이 기존 데이터를
      지우지 않게). 멱등 — 거래일 리포트에 다시 돌려도 같은 값.

    리포트 파일이나 그날 스냅샷이 없으면 아무것도 쓰지 않고 None 반환.
    """
    report_path = Path(report_root) / f"daily-{date}.json"
    if not report_path.exists():
        return None
    snaps = parse_topcap_for_date(date, root=snapshot_root)
    if not snaps.series:
        return None
    report = load_daily_report(report_path)

    names = dict(report.discovery_chosen_names)
    for b in report.blocked_outcomes:
        if b.name:
            names.setdefault(b.symbol, b.name)
    old_blocked = {b.symbol: b for b in report.blocked_outcomes}
    blocked_outcomes = []
    for new in _build_blocked_outcomes(report.discovery_blocked, names, snaps):
        old = old_blocked.get(new.symbol)
        blocked_outcomes.append(
            replace(
                new,
                intraday_pct=_prefer(new.intraday_pct, old.intraday_pct if old else None),
                day_change_pct=_prefer(new.day_change_pct, old.day_change_pct if old else None),
            )
        )

    buys = [
        replace(
            new,
            change_pct_at_entry=_prefer(new.change_pct_at_entry, old.change_pct_at_entry),
            rank_at_entry=_prefer(new.rank_at_entry, old.rank_at_entry),
        )
        for new, old in zip(_enrich_buys(report.buys, snaps), report.buys, strict=True)
    ]
    round_trips = [
        replace(
            new,
            mfe_pct=_prefer(new.mfe_pct, old.mfe_pct),
            mae_pct=_prefer(new.mae_pct, old.mae_pct),
        )
        for new, old in zip(
            _enrich_round_trips(report.round_trips, snaps), report.round_trips, strict=True
        )
    ]

    updated = replace(report, buys=buys, round_trips=round_trips, blocked_outcomes=blocked_outcomes)
    return save_daily_report(updated, root=report_root)


def _prefer[T](new: T | None, old: T | None) -> T | None:
    """새 조인 값 우선, 결손(None)이면 기존 값 유지."""
    return new if new is not None else old
