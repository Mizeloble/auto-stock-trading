"""실거래 전환 전 자동 안전 체크리스트.

29단계 PR8 (#17) — todo-list 의 "실거래 전환" 항목을 *기계가 강제* 한다.
사람이 잊을 위험을 줄이고, 통과한 게이트만 일관되게 측정.

각 게이트는 ``GateResult(name, ok, detail, skipped)`` 를 반환하는 *순수* 또는
*read-only* 함수. 외부 호출 없이 디스크/yaml 만 읽음. 이번 PR 의 책임은
*판정* 까지. CLI 출력·exit code 는 cli.py 가 결합.

활성 게이트 (6):
- ``check_risk_limits_active`` — daytrade YAML 의 손절·일일캡·kill 활성 여부
- ``check_no_pending_orders`` — 주문 저널에 결과 미확인 이벤트가 없는지
- ``check_heartbeat_recent`` — heartbeat 파일 mtime 이 max_age 안
- ``check_recent_paper_day_mdd`` — 최근 N일 일일 손실률의 누적 최저가 임계 안
- ``check_latest_backtest_sharpe`` — 가장 최근 백테스트의 Sharpe ≥ 임계
- ``check_kill_switch_simulation`` — 강제 -3% 시나리오에서 kill-switch 가 실제로 발화하는지

미구현 placeholder (이번 PR 에선 skipped=True 로 표시, 향후 PR 에서 활성):
- corporate action 캐시

설계 원칙:
- 한 게이트 실패가 다른 게이트 평가를 막지 않는다 — 모두 독립.
- ``skipped=True`` 는 ``ok`` 와 별개 — 사용자에게 "미구현" 임을 명시.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from .account.order_journal import OrderJournal
from .account.recovery import find_suspect_orders
from .heartbeat import check_heartbeat
from .llm.cost_tracker import CostTracker
from .risk.manager import AccountState, RiskLimits, RiskManager

__all__ = [
    "GateResult",
    "check_heartbeat_recent",
    "check_kill_switch_simulation",
    "check_latest_backtest_sharpe",
    "check_llm_cost_within_budget",
    "check_no_pending_orders",
    "check_recent_paper_day_mdd",
    "check_risk_limits_active",
    "run_all_gates",
]


@dataclass(frozen=True)
class GateResult:
    """단일 게이트 평가 결과.

    - ``ok``: 통과 여부. ``skipped`` 면 의미 없음 (False 를 default 로 둠).
    - ``detail``: 통과 시 짧은 fact, 실패 시 다음 액션. CLI 표 한 줄.
    - ``skipped``: 다른 PR 의존이거나 환경 미충족 — "활성 게이트" 와 분리.
    """

    name: str
    ok: bool
    detail: str
    skipped: bool = False


# ---- 활성 게이트 -------------------------------------------------------------


def check_risk_limits_active(profile_path: Path) -> GateResult:
    """daytrade YAML 의 손절·일일캡·kill 한도가 활성인지.

    실거래 전 *최소* 활성: ``stop_loss_pct > 0`` AND ``daily_loss_kill is True``.
    ``daily_notional_limit`` / ``max_order_value`` 는 권장(자본 규모 의존).
    """
    name = "risk_limits_active"
    if not profile_path.exists():
        return GateResult(
            name=name,
            ok=False,
            detail=f"프로파일 파일 없음: {profile_path}",
        )
    try:
        raw = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        return GateResult(name=name, ok=False, detail=f"YAML 파싱 실패: {exc}")
    risk = raw.get("risk") or {}
    stop_loss = float(risk.get("stop_loss_pct") or 0)
    daily_kill = bool(risk.get("daily_loss_kill"))
    if stop_loss > 0 and daily_kill:
        return GateResult(
            name=name,
            ok=True,
            detail=f"stop_loss_pct={stop_loss}, daily_loss_kill=True",
        )
    missing = []
    if stop_loss <= 0:
        missing.append("stop_loss_pct=0")
    if not daily_kill:
        missing.append("daily_loss_kill=False")
    return GateResult(
        name=name,
        ok=False,
        detail=f"한도 미활성: {', '.join(missing)} — 프로파일에서 활성화 필요",
    )


def check_no_pending_orders(journal_path: Path) -> GateResult:
    """주문 저널에 submit-only(결과 미확인) 항목이 없는지.

    있으면 #9 PR4 (자동 매칭) 가 끝날 때까지 사용자가 직접 키움 HTS 에서 확인.
    """
    name = "no_pending_orders"
    if not journal_path.exists():
        return GateResult(name=name, ok=True, detail="저널 없음 (실행 이력 0)")
    journal = OrderJournal(path=journal_path)
    suspects = find_suspect_orders(journal.replay())
    if not suspects:
        return GateResult(name=name, ok=True, detail="결과 미확인 0건")
    return GateResult(
        name=name,
        ok=False,
        detail=f"결과 미확인 {len(suspects)}건 — `auto-trade orders inspect` 후 정리",
    )


def check_heartbeat_recent(heartbeat_path: Path, max_age_sec: int = 86_400) -> GateResult:
    """heartbeat 파일이 max_age_sec(기본 1일) 안에 갱신된 적 있는지.

    실거래 전엔 최소 1주일 paper-day 운영을 권장하므로 1일 이내 갱신은 헐거운
    기준. 운영 검증 단계에서 더 엄격하게 잡으려면 ``max_age_sec=3600`` 으로.
    """
    name = "heartbeat_recent"
    status = check_heartbeat(heartbeat_path, max_age_sec)
    if status.ok:
        return GateResult(
            name=name,
            ok=True,
            detail=f"age={status.age_sec:.0f}s",
        )
    if status.reason == "missing":
        return GateResult(
            name=name,
            ok=False,
            detail=f"heartbeat 파일 없음: {heartbeat_path} — paper-day 1회 이상 시동 필요",
        )
    return GateResult(
        name=name,
        ok=False,
        detail=f"갱신 누락: age={status.age_sec:.0f}s > {max_age_sec}s",
    )


def check_recent_paper_day_mdd(
    reports_root: Path, days: int = 30, threshold: float = -0.05
) -> GateResult:
    """최근 N일 daily-*.json 의 ``realized_pnl_pct`` 누적 최저점이 임계 안인지.

    정확한 MDD 는 일별 누적 시계열 + cummax 가 필요하지만, paper-day 의 일일
    리포트는 누적 에쿼티가 아닌 *그날의 실현 PnL %* 만 가짐. 따라서 단순한
    근사: 최근 N일의 일일 손익률 시계열에서 가장 큰 단일 일 손실을 쓴다.

    실거래 전 게이트로서 *근사* 도 충분 — 최악의 하루가 -5% 이내면 OK,
    더 정밀한 측정은 #1 walk-forward 결과(별도 모듈) 에서.
    """
    name = "recent_paper_day_mdd"
    if not reports_root.exists():
        return GateResult(
            name=name,
            ok=False,
            detail=f"리포트 디렉터리 없음: {reports_root} — paper-day 며칠 이상 운영 필요",
        )
    files = sorted(reports_root.glob("daily-*.json"))
    if not files:
        return GateResult(
            name=name,
            ok=False,
            detail="paper-day 일일 리포트 없음 — 실거래 전 며칠 이상 운영 필요",
        )
    recent = files[-days:]
    pnl_pcts: list[float] = []
    for f in recent:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            continue
        pct = data.get("realized_pnl_pct")
        if pct is None:
            continue
        # JSON 에 % 단위(예: -1.5) 인지 비율(-0.015) 인지 — daily.py 의 출력 그대로 본다.
        # save_daily_report 가 % 로 저장 → 비율로 환산.
        pnl_pcts.append(float(pct) / 100.0)
    if not pnl_pcts:
        return GateResult(
            name=name,
            ok=False,
            detail=f"최근 {days}일 리포트에 realized_pnl_pct 없음",
        )
    worst = min(pnl_pcts)
    if worst >= threshold:
        return GateResult(
            name=name,
            ok=True,
            detail=f"최근 {len(pnl_pcts)}일 최악 일일 손실 {worst:.2%} ≥ {threshold:.0%}",
        )
    return GateResult(
        name=name,
        ok=False,
        detail=f"최근 {len(pnl_pcts)}일 최악 일일 손실 {worst:.2%} < {threshold:.0%}",
    )


def check_latest_backtest_sharpe(backtests_root: Path, threshold: float = 0.0) -> GateResult:
    """가장 최근 백테스트의 Sharpe ≥ threshold.

    `data/backtests/{run}/report.json` 형태로 저장된다 (engine.save_result).
    여러 폴더가 있으면 mtime 가장 최근 1개.
    """
    name = "latest_backtest_sharpe"
    if not backtests_root.exists():
        return GateResult(
            name=name,
            ok=False,
            detail=f"백테스트 디렉터리 없음: {backtests_root}",
        )
    runs = [p for p in backtests_root.iterdir() if p.is_dir()]
    if not runs:
        return GateResult(
            name=name,
            ok=False,
            detail="백테스트 결과 폴더 없음 — `auto-trade backtest` 1회 이상 실행 필요",
        )
    latest = max(runs, key=lambda p: p.stat().st_mtime)
    report = latest / "report.json"
    if not report.exists():
        return GateResult(
            name=name,
            ok=False,
            detail=f"최근 백테스트에 report.json 없음: {latest.name}",
        )
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return GateResult(name=name, ok=False, detail=f"백테스트 리포트 파싱 실패: {exc}")
    sharpe = float(data.get("sharpe") or 0)
    if sharpe >= threshold:
        return GateResult(
            name=name,
            ok=True,
            detail=f"{latest.name}: Sharpe={sharpe:.3f} ≥ {threshold}",
        )
    return GateResult(
        name=name,
        ok=False,
        detail=f"{latest.name}: Sharpe={sharpe:.3f} < {threshold}",
    )


def check_llm_cost_within_budget(
    cost_path: Path | None,
    monthly_budget_usd: float = 5.0,
) -> GateResult:
    """29단계 PR15 (#16) — LLM 월 한도 내 누적 검증.

    `cost_path` None 이면 비활성 환경 — skipped 로 표시 (cost 추적 자체가
    꺼져있는 상태). 활성이면 실 게이트로 동작.
    """
    name = "llm_cost_within_budget"
    if cost_path is None:
        return GateResult(
            name=name,
            ok=False,
            detail="비활성 — `.env` 의 LLM_COST_PATH 설정 시 활성",
            skipped=True,
        )
    tracker = CostTracker(path=cost_path, monthly_budget_usd=monthly_budget_usd)
    status = tracker.budget_status()
    if status.over:
        return GateResult(
            name=name,
            ok=False,
            detail=(
                f"{status.year_month}: ${status.spent_usd:.2f} / ${status.budget_usd:.2f} "
                f"({status.pct_used:.0%}) — 한도 초과"
            ),
        )
    if status.alert:
        return GateResult(
            name=name,
            ok=True,
            detail=(
                f"{status.year_month}: ${status.spent_usd:.2f} / ${status.budget_usd:.2f} "
                f"({status.pct_used:.0%}) — 알림 임계 도달, 실거래 가능"
            ),
        )
    return GateResult(
        name=name,
        ok=True,
        detail=f"{status.year_month}: ${status.spent_usd:.2f} / ${status.budget_usd:.2f} ({status.pct_used:.0%})",
    )


def check_kill_switch_simulation(profile_path: Path) -> GateResult:
    """강제 -3% 시나리오 → kill-switch 가 실제로 발화하는지 + 정상 시 오발화 안 하는지.

    무엇/왜:
    - kill-switch 는 "실거래에서 최악의 하루 손실 캡". 코드 회귀로 조용히 깨져있으면
      막상 -3% 도달했을 때 발화 안 함 — 가장 위험한 침묵 실패.
    - 외부 호출 없는 *순수* 시뮬. 프로파일의 ``daily_loss_kill`` + ``daily_loss_limit``
      을 그대로 ``RiskLimits`` 에 로드 → 가상 AccountState 둘 (한도 *초과* / *미달*) 로
      ``should_force_liquidate()`` 가 양쪽 모두 기대대로 동작하는지 확인.
    - 한도 초과에서 발화 안 하거나, 한도 미달에서 오발화하면 ✗ 로 명확히 분리해 보고.

    profile 의 ``daily_loss_kill`` 자체가 False 면 시뮬 자체가 무의미 — fail
    (사용자가 활성화하라는 메시지). ``check_risk_limits_active`` 와 의도적으로
    중복: 그 게이트는 *설정* 만 보고, 이 게이트는 *실제 코드 동작* 까지 확인.
    """
    name = "kill_switch_simulation"
    if not profile_path.exists():
        return GateResult(name=name, ok=False, detail=f"프로파일 파일 없음: {profile_path}")
    try:
        raw = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        return GateResult(name=name, ok=False, detail=f"YAML 파싱 실패: {exc}")
    risk = raw.get("risk") or {}
    if not bool(risk.get("daily_loss_kill")):
        return GateResult(
            name=name,
            ok=False,
            detail="daily_loss_kill=False — 시뮬 불가, 프로파일에서 활성화 필요",
        )
    raw_limit = risk.get("daily_loss_limit")
    daily_loss_limit = float(raw_limit) if raw_limit is not None else 0.03
    if daily_loss_limit <= 0:
        return GateResult(
            name=name,
            ok=False,
            detail=f"daily_loss_limit={daily_loss_limit} — 양수 필요",
        )
    limits = RiskLimits(daily_loss_kill=True, daily_loss_limit=daily_loss_limit)
    manager = RiskManager(limits=limits)
    # 한도 초과 시나리오 (한도의 1.5 배 손실) — 발화 기대.
    breach = AccountState(
        deposit=0.0,
        total_equity=100.0 * (1.0 - daily_loss_limit * 1.5),
        day_start_equity=100.0,
    )
    if not manager.should_force_liquidate(breach):
        return GateResult(
            name=name,
            ok=False,
            detail=(
                f"한도 -{daily_loss_limit:.1%} 초과(-{daily_loss_limit * 1.5:.1%})에서 "
                f"미발화 — kill-switch 로직 깨짐"
            ),
        )
    # 한도 미달 시나리오 (한도의 0.5 배 손실) — 발화 X 기대.
    safe = AccountState(
        deposit=0.0,
        total_equity=100.0 * (1.0 - daily_loss_limit * 0.5),
        day_start_equity=100.0,
    )
    if manager.should_force_liquidate(safe):
        return GateResult(
            name=name,
            ok=False,
            detail=(
                f"한도 -{daily_loss_limit:.1%} 미달(-{daily_loss_limit * 0.5:.1%})에서 "
                f"오발화 — 임계 비교 깨짐"
            ),
        )
    return GateResult(
        name=name,
        ok=True,
        detail=f"daily_loss_limit=-{daily_loss_limit:.1%} 시뮬 통과 (초과 발화 + 미달 보존)",
    )


# ---- 미구현 placeholder ------------------------------------------------------


_SKIPPED_GATES: tuple[tuple[str, str], ...] = (
    # multi_channel_alerts (#12) 는 사용자가 단일 채널(텔레그램) 운영 의향 — 게이트 제거.
    # 추후 Slack/이메일 등 fallback 채널 도입 시 별도 게이트로 재추가.
    ("corporate_action_cache_fresh", "PR 미구현 (#11) — KRX 액면분할/배당락 캐시 7일 이내"),
)


def _build_skipped_results() -> list[GateResult]:
    return [
        GateResult(name=name, ok=False, detail=detail, skipped=True)
        for name, detail in _SKIPPED_GATES
    ]


# ---- 합치기 ------------------------------------------------------------------


def run_all_gates(
    *,
    profile_path: Path,
    journal_path: Path,
    heartbeat_path: Path,
    reports_root: Path,
    backtests_root: Path,
    llm_cost_path: Path | None = None,
    llm_monthly_budget_usd: float = 5.0,
) -> list[GateResult]:
    """모든 게이트 실행 → 결과 리스트 (선언 순서대로).

    한 게이트의 예외가 전체를 막지 않게 *각 호출 try* — 예상치 못한 환경
    오류는 GateResult(ok=False, detail=str(exc)) 로 fail.
    """
    results: list[GateResult] = []
    for fn, args in (
        (check_risk_limits_active, (profile_path,)),
        (check_no_pending_orders, (journal_path,)),
        (check_heartbeat_recent, (heartbeat_path,)),
        (check_recent_paper_day_mdd, (reports_root,)),
        (check_latest_backtest_sharpe, (backtests_root,)),
        (check_kill_switch_simulation, (profile_path,)),
        (check_llm_cost_within_budget, (llm_cost_path, llm_monthly_budget_usd)),
    ):
        try:
            results.append(fn(*args))
        except Exception as exc:
            results.append(
                GateResult(name=fn.__name__.removeprefix("check_"), ok=False, detail=f"예외: {exc}")
            )
    results.extend(_build_skipped_results())
    return results
