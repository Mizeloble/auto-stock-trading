"""실거래 전환 전 자동 안전 체크리스트.

29단계 PR8 (#17) — todo-list 의 "실거래 전환" 항목을 *기계가 강제* 한다.
사람이 잊을 위험을 줄이고, 통과한 게이트만 일관되게 측정.

각 게이트는 ``GateResult(name, ok, detail, skipped)`` 를 반환하는 *순수* 또는
*read-only* 함수. 외부 호출 없이 디스크/yaml 만 읽음. 이번 PR 의 책임은
*판정* 까지. CLI 출력·exit code 는 cli.py 가 결합.

활성 게이트 (5):
- ``check_risk_limits_active`` — daytrade YAML 의 손절·일일캡·kill 활성 여부
- ``check_no_pending_orders`` — 주문 저널에 결과 미확인 이벤트가 없는지
- ``check_heartbeat_recent`` — heartbeat 파일 mtime 이 max_age 안
- ``check_recent_paper_day_mdd`` — 최근 N일 일일 손실률의 누적 최저가 임계 안
- ``check_latest_backtest_sharpe`` — 가장 최근 백테스트의 Sharpe ≥ 임계

미구현 placeholder (이번 PR 에선 skipped=True 로 표시, 향후 PR 에서 활성):
- kill-switch 시뮬 / 다채널 알림 ack / LLM 비용 누적 / corporate action 캐시

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

__all__ = [
    "GateResult",
    "check_heartbeat_recent",
    "check_latest_backtest_sharpe",
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


# ---- 미구현 placeholder ------------------------------------------------------


_SKIPPED_GATES: tuple[tuple[str, str], ...] = (
    ("kill_switch_simulation", "PR 미구현 — 강제 -3% 시나리오로 kill-switch 발화 검증"),
    ("multi_channel_alerts", "PR 미구현 (#12) — 텔레그램 외 fallback 채널 ack"),
    ("llm_cost_within_budget", "PR 미구현 (#16) — 월 $5 한도 내 누적 검증"),
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
    ):
        try:
            results.append(fn(*args))
        except Exception as exc:
            results.append(
                GateResult(name=fn.__name__.removeprefix("check_"), ok=False, detail=f"예외: {exc}")
            )
    results.extend(_build_skipped_results())
    return results
