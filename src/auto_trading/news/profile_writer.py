"""24단계 Phase C — DaytradeProposal → daytrade-current.yaml 자동 갱신.

흐름:
1. 기존 `strategies/profiles/daytrade-current.yaml` 로드 (없으면 daytrade-default.yaml).
2. proposal 의 None 아닌 필드만 적용 + 안전 캡 검사.
3. 안전 캡 벗어나는 변경은 거부 + 경고 메시지 (자동화 폭주 방지).
4. 기존 yaml 을 `daytrade-current.yaml.bak.YYYY-MM-DD` 로 백업 후 새 yaml 저장.
5. 변경 diff 를 한국어 메시지로 반환 → 호출자(Phase D 토요일 cron) 가 텔레그램 푸시.

이 모듈은 LLM 호출 안 함 — 그건 Phase B 책임. 결정론적: 같은 proposal + 같은
current_profile → 같은 결과.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import datetime
from pathlib import Path

import structlog

from .._common import KST
from ..daytrade.profile import DaytradeProfile
from ..reports.daily import with_symbol_name
from .proposal import DaytradeProposal

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SafetyCaps:
    """LLM 자동화 폭주 방지 — 기본값은 todo-list Phase C 의 보수 한도."""

    top_n_max: int = 5
    stop_loss_pct_max: float = 0.03
    trailing_stop_pct_max: float = 0.03
    partial_take_pct_max: float = 0.05
    momentum_threshold_pct_max: float = 3.0
    cooldown_min_max: int = 120
    momentum_lookback_min_max: int = 30
    # daily_loss_kill 은 자동 변경을 아예 막지는 않지만 "사람 명시" 권장.
    block_daily_loss_kill_change: bool = False


@dataclass
class ApplyResult:
    """proposal 적용 결과 — Phase D cron 이 텔레그램으로 그대로 푸시."""

    applied_profile: DaytradeProfile
    accepted: list[str] = field(default_factory=list)  # "top_n: 3 → 2" 형식
    rejected: list[str] = field(default_factory=list)  # "stop_loss_pct 0.05 거부 (캡 0.03)"
    backup_path: Path | None = None
    written_path: Path | None = None


# 모든 변경 가능 필드 — DaytradeProposal 과 1:1.
_PROPOSAL_FIELDS_TOP_LEVEL = ("top_n",)
_PROPOSAL_FIELDS_PARAMS = (
    "cooldown_min",
    "momentum_lookback_min",
    "momentum_threshold_pct",
)
_PROPOSAL_FIELDS_RISK = (
    "stop_loss_pct",
    "trailing_stop_pct",
    "partial_take_pct",
    "partial_take_qty_ratio",
    "daily_loss_kill",
)


def apply_proposal(
    proposal: DaytradeProposal,
    current: DaytradeProfile,
    *,
    caps: SafetyCaps | None = None,
) -> ApplyResult:
    """proposal 의 None 아닌 필드를 current 에 적용. 안전 캡 위반은 거부.

    무엇/왜:
    - LLM 이 잘못된 값을 제안해도 자동 적용을 막는 1차 방어선.
    - 거부된 변경은 rejected 리스트에 한국어로 기록 → 사용자가 토요일 알림 보고 판단.
    - 새 프로파일은 last_updated_at 갱신 (created_at 은 보존).

    어떻게 확인: tests/test_profile_writer.py 가 다양한 proposal 조합 검증.
    """
    caps = caps or SafetyCaps()
    accepted: list[str] = []
    rejected: list[str] = []

    base = current.model_dump(mode="python")
    new_top: dict = dict(base)
    new_params: dict = dict(base.get("params") or {})
    new_risk: dict = dict(base.get("risk") or {})

    # top_n
    proposed_top_n = proposal.top_n
    if proposed_top_n is not None:
        if proposed_top_n > caps.top_n_max:
            rejected.append(f"top_n {proposed_top_n} 거부 (캡 {caps.top_n_max})")
        elif proposed_top_n != current.top_n:
            accepted.append(f"top_n: {current.top_n} → {proposed_top_n}")
            new_top["top_n"] = proposed_top_n

    # params
    for fname in _PROPOSAL_FIELDS_PARAMS:
        new_v = getattr(proposal, fname)
        if new_v is None:
            continue
        cap_attr = f"{fname}_max"
        cap_val = getattr(caps, cap_attr, None)
        if cap_val is not None and new_v > cap_val:
            rejected.append(f"{fname} {new_v} 거부 (캡 {cap_val})")
            continue
        old_v = new_params.get(fname)
        if old_v != new_v:
            accepted.append(f"{fname}: {old_v} → {new_v}")
            new_params[fname] = new_v

    # risk
    for fname in _PROPOSAL_FIELDS_RISK:
        new_v = getattr(proposal, fname)
        if new_v is None:
            continue
        if fname == "daily_loss_kill":
            if caps.block_daily_loss_kill_change and new_v != new_risk.get(fname):
                rejected.append(f"daily_loss_kill {new_v} 거부 (자동 변경 차단)")
                continue
            old_v = new_risk.get(fname)
            if old_v != new_v:
                accepted.append(f"daily_loss_kill: {old_v} → {new_v}")
                new_risk[fname] = new_v
            continue
        cap_attr = f"{fname}_max"
        cap_val = getattr(caps, cap_attr, None)
        if cap_val is not None and new_v > cap_val:
            rejected.append(f"{fname} {new_v} 거부 (캡 {cap_val})")
            continue
        old_v = new_risk.get(fname)
        if old_v != new_v:
            accepted.append(f"{fname}: {old_v} → {new_v}")
            new_risk[fname] = new_v

    new_top["params"] = new_params
    new_top["risk"] = new_risk
    new_top["last_updated_at"] = datetime.now(KST)
    if "created_at" not in new_top or new_top.get("created_at") is None:
        new_top["created_at"] = current.created_at or datetime.now(KST)
    applied = DaytradeProfile.model_validate(new_top)
    return ApplyResult(applied_profile=applied, accepted=accepted, rejected=rejected)


def write_with_backup(
    profile: DaytradeProfile,
    target_path: Path,
    *,
    backup_dir: Path | None = None,
    today: date_cls | None = None,
) -> Path | None:
    """target_path 가 존재하면 .bak.YYYY-MM-DD 로 백업 후 profile.dump_yaml 로 작성.

    - backup_dir 이 None 이면 target_path 와 같은 디렉터리에 .bak 파일.
    - 같은 날 두 번 호출하면 같은 .bak 가 덮어쓰여짐 — 토요일 cron 1회 호출 가정이라 OK.
    - 반환값: 만들어진 백업 경로 (없었으면 None).
    """
    backup_path: Path | None = None
    if target_path.exists():
        day = today or datetime.now(KST).date()
        bak_root = backup_dir or target_path.parent
        bak_root.mkdir(parents=True, exist_ok=True)
        backup_path = bak_root / f"{target_path.name}.bak.{day.isoformat()}"
        shutil.copy2(target_path, backup_path)
    profile.dump_yaml(target_path)
    return backup_path


def apply_and_persist(
    proposal: DaytradeProposal,
    *,
    current_path: Path,
    target_path: Path,
    fallback_path: Path | None = None,
    caps: SafetyCaps | None = None,
    today: date_cls | None = None,
) -> ApplyResult:
    """1차 진입점 — 토요일 cron 또는 수동 호출이 부른다.

    무엇/왜:
    - current_path 로드 → 없으면 fallback_path (daytrade-default.yaml) → 둘 다 없으면
      DaytradeProfile() 기본 인스턴스로 시작.
    - apply_proposal 로 안전 캡 통과한 변경만 반영.
    - write_with_backup 으로 백업 + 새 yaml 저장.
    - 결과(ApplyResult) 의 accepted/rejected 를 호출자가 텔레그램 푸시.
    """
    base = _load_or_default(current_path, fallback_path)
    result = apply_proposal(proposal, base, caps=caps)
    backup = write_with_backup(result.applied_profile, target_path, today=today)
    result.backup_path = backup
    result.written_path = target_path
    log.info(
        "news.profile_writer.applied",
        target=str(target_path),
        accepted=len(result.accepted),
        rejected=len(result.rejected),
    )
    return result


def _load_or_default(current_path: Path, fallback_path: Path | None) -> DaytradeProfile:
    if current_path.exists():
        return DaytradeProfile.load_yaml(current_path)
    if fallback_path is not None and fallback_path.exists():
        return DaytradeProfile.load_yaml(fallback_path)
    return DaytradeProfile(name="auto")


def format_apply_message(
    result: ApplyResult,
    proposal: DaytradeProposal,
    *,
    symbol_names: dict[str, str] | None = None,
) -> str:
    """텔레그램 푸시용 한국어. accepted=[] + rejected=[] 도 안전 처리.

    `symbol_names` — 회피 종목 코드를 "코드 종목명" 으로 표시할 코드→이름 맵.
    이름이 없는 코드는 코드만(폴백).
    """
    names = symbol_names or {}
    lines = ["🗞 다음 주 day-trade 프로파일 갱신", "", proposal.summary_ko]
    if result.accepted:
        lines.append("")
        lines.append("✅ 적용된 변경")
        for line in result.accepted:
            lines.append(f"• {line}")
    else:
        lines.append("")
        lines.append("✅ 적용된 변경: 없음 (모두 동일하거나 거부됨)")
    if result.rejected:
        lines.append("")
        lines.append("⚠️ 거부된 변경 (안전 캡)")
        for line in result.rejected:
            lines.append(f"• {line}")
    if proposal.themes_to_watch:
        lines.append("")
        lines.append("📡 주목 테마: " + ", ".join(proposal.themes_to_watch))
    if proposal.symbols_to_avoid:
        lines.append(
            "⛔ 회피 종목: "
            + ", ".join(with_symbol_name(s, names.get(s)) for s in proposal.symbols_to_avoid)
        )
    lines.append("")
    lines.append("📝 근거")
    lines.append(proposal.rationale_ko)
    lines.append(f"신뢰도: {proposal.confidence:.2f}")
    if result.backup_path is not None:
        lines.append(f"이전 프로파일 백업: {result.backup_path.name}")
    return "\n".join(lines)


__all__ = [
    "ApplyResult",
    "SafetyCaps",
    "apply_and_persist",
    "apply_proposal",
    "format_apply_message",
    "write_with_backup",
]
