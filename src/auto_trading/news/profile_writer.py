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
from ..reports.daily import DailyReport, with_symbol_name
from ..strategy.scanner import RegimeReport
from .glossary import expand_change_with_glossary
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


def format_inputs_block(
    *,
    reports: list[DailyReport],
    news_total: int,
    news_to_llm: int,
    regime: RegimeReport | None,
) -> str:
    """LLM 결정의 입력을 결정론적 한국어 블록으로.

    무엇/왜:
    - LLM 본문이 인용한 숫자(매수 N건, ATR X%) 가 정말 입력 데이터에 있는지
      사용자가 직접 검증할 수 있도록, 봇이 보낸 원자료를 그대로 노출.
    - 헛소리 차단 + 결정의 투명성 ↑.

    어떻게 확인: 단위 테스트 ``test_format_inputs_block_*`` 가 각 항목 산출 검증.
    데이터 부재 시 "정보 없음" 폴백 — 메시지가 깨지지 않음.
    """
    lines = ["📊 이번 결정의 입력"]
    if reports:
        start = reports[0].date
        end = reports[-1].date
        lines.append(f"• 기간: {start} ~ {end} ({len(reports)}일)")
        total_buys = sum(len(r.buys) for r in reports)
        total_liqs = sum(len(r.liquidations) for r in reports)
        total_errors = sum(r.error_count for r in reports)
        lines.append(f"• 매수 {total_buys}건 / 청산 {total_liqs}건 / 에러 {total_errors}건")
        with_pnl = [r for r in reports if r.realized_pnl_after_cost is not None]
        if with_pnl:
            avg_pnl = sum(r.realized_pnl_after_cost or 0.0 for r in with_pnl) / len(with_pnl)
            lines.append(f"• 실현손익 표본 {len(with_pnl)}일, 비용 차감 후 평균 {avg_pnl:+,.0f}원")
        turnovers = [r.turnover_ratio for r in reports if r.turnover_ratio is not None]
        if turnovers:
            avg_turn = sum(turnovers) / len(turnovers)
            lines.append(f"• 평균 회전율 {avg_turn * 100:.1f}%")
    else:
        lines.append("• 일일 리포트 0건 — 데이터 부족")

    if regime is not None:
        lines.append(f"• 시장국면: {regime.regime.value} — {regime.summary_ko}")
    else:
        lines.append("• 시장국면: 정보 없음")

    if news_total > 0:
        lines.append(f"• 입력 뉴스: 수집 {news_total}건 → LLM 에 {news_to_llm}건 전달")
    else:
        lines.append("• 입력 뉴스: 수집 0건")
    return "\n".join(lines)


def _format_rationale_block(proposal: DaytradeProposal) -> list[str]:
    """proposal 의 4섹션 근거를 줄 리스트로. 빈 섹션은 자동 생략."""
    out = ["📝 근거"]
    if proposal.signals_ko:
        out.append("📍 시그널")
        for s in proposal.signals_ko:
            out.append(f"  • {s}")
    if proposal.judgment_ko:
        out.append("🎯 판단")
        out.append(f"  {proposal.judgment_ko}")
    if proposal.referenced_headlines_ko:
        out.append("🗞 영향 헤드라인")
        for h in proposal.referenced_headlines_ko:
            out.append(f"  • {h}")
    if proposal.weekly_memo_ko:
        out.append("📅 다음 주 메모")
        out.append(f"  {proposal.weekly_memo_ko}")
    return out


def _extract_change_key(accepted_line: str) -> str | None:
    """``"key: old → new"`` 에서 key 만. 파싱 실패 시 None."""
    head, sep, _ = accepted_line.partition(":")
    if not sep:
        return None
    return head.strip()


def format_apply_message(
    result: ApplyResult,
    proposal: DaytradeProposal,
    *,
    symbol_names: dict[str, str] | None = None,
    inputs_block: str | None = None,
) -> str:
    """텔레그램 푸시용 한국어. accepted=[] + rejected=[] 도 안전 처리.

    구성 순서:
    1. 헤드라인 + summary_ko
    2. 📊 정량 입력 (inputs_block 가 주어진 경우 — 결정론적 검증용)
    3. ✅ 적용된 변경 (글로서리 풀이 + 변경별 LLM 이유)
    4. ⚠️ 거부된 변경 (안전 캡 위반)
    5. 📡 주목 테마 / ⛔ 회피 종목
    6. 📝 근거 (시그널 / 판단 / 영향 헤드라인 / 다음 주 메모)
    7. 신뢰도 + 백업 경로

    `symbol_names` — 회피 종목 코드를 "코드 종목명" 으로 표시할 코드→이름 맵.
    `inputs_block` — ``format_inputs_block`` 결과 텍스트. None 이면 섹션 생략.
    """
    names = symbol_names or {}
    lines = ["🗞 다음 주 day-trade 프로파일 갱신", "", proposal.summary_ko]
    if inputs_block:
        lines.append("")
        lines.append(inputs_block)
    if result.accepted:
        lines.append("")
        lines.append("✅ 적용된 변경")
        for accepted_line in result.accepted:
            lines.extend(expand_change_with_glossary(accepted_line))
            key = _extract_change_key(accepted_line)
            reason = proposal.change_reasons.get(key) if key else None
            if reason:
                lines.append(f"    📌 이번에 바꾼 이유: {reason}")
    else:
        lines.append("")
        lines.append("✅ 적용된 변경: 없음 (모두 동일하거나 거부됨)")
    if result.rejected:
        lines.append("")
        lines.append("⚠️ 거부된 변경 (안전 캡)")
        for rejected_line in result.rejected:
            lines.append(f"• {rejected_line}")
    if proposal.themes_to_watch:
        lines.append("")
        lines.append("📡 주목 테마: " + ", ".join(proposal.themes_to_watch))
    if proposal.symbols_to_avoid:
        lines.append(
            "⛔ 회피 종목: "
            + ", ".join(with_symbol_name(s, names.get(s)) for s in proposal.symbols_to_avoid)
        )
    lines.append("")
    lines.extend(_format_rationale_block(proposal))
    lines.append("")
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
    "format_inputs_block",
    "write_with_backup",
]
