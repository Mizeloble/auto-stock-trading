"""24단계 Phase C — apply_proposal + write_with_backup + apply_and_persist 단위."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from auto_trading._common import KST
from auto_trading.daytrade.profile import (
    DaytradeProfile,
    DaytradeRiskLimits,
    DaytradeStrategyParams,
)
from auto_trading.news.profile_writer import (
    SafetyCaps,
    apply_and_persist,
    apply_proposal,
    format_apply_message,
    write_with_backup,
)
from auto_trading.news.proposal import DaytradeProposal


def _base_profile() -> DaytradeProfile:
    return DaytradeProfile(
        name="t",
        strategy="index_momentum_daytrade",
        top_n=3,
        params=DaytradeStrategyParams(
            cooldown_min=30,
            momentum_lookback_min=5,
            momentum_threshold_pct=0.0,
        ),
        risk=DaytradeRiskLimits(
            stop_loss_pct=0.01,
            trailing_stop_pct=0.005,
            partial_take_pct=0.015,
            partial_take_qty_ratio=0.5,
            daily_loss_kill=False,
        ),
    )


def test_apply_no_changes_when_proposal_all_none() -> None:
    """모든 옵션 None — accepted/rejected 둘 다 비어있고 프로파일 동일."""
    prop = DaytradeProposal(summary_ko="유지", rationale_ko="ok", confidence=0.5)
    base = _base_profile()
    out = apply_proposal(prop, base)
    assert out.accepted == []
    assert out.rejected == []
    # last_updated_at 만 갱신될 뿐 핵심 값은 동일.
    assert out.applied_profile.top_n == base.top_n
    assert out.applied_profile.risk.stop_loss_pct == base.risk.stop_loss_pct


def test_apply_top_n_change_recorded() -> None:
    prop = DaytradeProposal(summary_ko="x", rationale_ko="y", confidence=0.5, top_n=2)
    out = apply_proposal(prop, _base_profile())
    assert out.applied_profile.top_n == 2
    assert any("top_n" in line and "→ 2" in line for line in out.accepted)


def test_apply_top_n_above_cap_rejected() -> None:
    """SafetyCaps.top_n_max=5 — 6 제안은 거부."""
    prop = DaytradeProposal(summary_ko="x", rationale_ko="y", confidence=0.5, top_n=6)
    out = apply_proposal(prop, _base_profile())
    assert out.applied_profile.top_n == 3  # 변경 X
    assert any("top_n 6 거부" in line for line in out.rejected)


def test_apply_stop_loss_above_cap_rejected_but_others_pass() -> None:
    """stop_loss 0.05 > 캡 0.03 거부, 같은 proposal 의 다른 변경은 그대로 적용."""
    prop = DaytradeProposal(
        summary_ko="x",
        rationale_ko="y",
        confidence=0.5,
        stop_loss_pct=0.05,  # 거부
        trailing_stop_pct=0.008,  # 통과
        cooldown_min=15,  # 통과
    )
    out = apply_proposal(prop, _base_profile())
    assert out.applied_profile.risk.stop_loss_pct == 0.01  # 변경 X
    assert out.applied_profile.risk.trailing_stop_pct == 0.008
    assert out.applied_profile.params.cooldown_min == 15
    assert any("stop_loss_pct" in line for line in out.rejected)
    assert any("trailing_stop_pct" in line for line in out.accepted)
    assert any("cooldown_min" in line for line in out.accepted)


def test_apply_daily_loss_kill_can_be_blocked() -> None:
    caps = SafetyCaps(block_daily_loss_kill_change=True)
    prop = DaytradeProposal(summary_ko="x", rationale_ko="y", confidence=0.5, daily_loss_kill=True)
    out = apply_proposal(prop, _base_profile(), caps=caps)
    assert out.applied_profile.risk.daily_loss_kill is False
    assert any("daily_loss_kill" in line for line in out.rejected)


def test_apply_daily_loss_kill_default_allows_change() -> None:
    """기본 caps 는 daily_loss_kill 변경 허용."""
    prop = DaytradeProposal(summary_ko="x", rationale_ko="y", confidence=0.5, daily_loss_kill=True)
    out = apply_proposal(prop, _base_profile())
    assert out.applied_profile.risk.daily_loss_kill is True


def test_apply_skips_field_when_value_unchanged() -> None:
    """제안값이 현재값과 같으면 accepted 에 안 들어감 (노이즈 방지)."""
    prop = DaytradeProposal(
        summary_ko="x", rationale_ko="y", confidence=0.5, top_n=3, stop_loss_pct=0.01
    )
    out = apply_proposal(prop, _base_profile())
    assert out.accepted == []
    assert out.rejected == []


def test_write_with_backup_creates_bak_when_target_exists(tmp_path: Path) -> None:
    """기존 yaml 이 있으면 .bak.YYYY-MM-DD 로 백업."""
    target = tmp_path / "daytrade-current.yaml"
    _base_profile().dump_yaml(target)
    new_profile = _base_profile()
    new_profile = new_profile.model_copy(update={"top_n": 4})
    backup = write_with_backup(new_profile, target, today=date(2026, 5, 7))
    assert backup is not None
    assert backup.name == "daytrade-current.yaml.bak.2026-05-07"
    # 새 파일 검증
    loaded = DaytradeProfile.load_yaml(target)
    assert loaded.top_n == 4


def test_write_with_backup_no_bak_when_target_missing(tmp_path: Path) -> None:
    """target 이 없으면 백업 없이 새로 생성."""
    target = tmp_path / "daytrade-current.yaml"
    backup = write_with_backup(_base_profile(), target, today=date(2026, 5, 7))
    assert backup is None
    assert target.exists()


def test_apply_and_persist_full_cycle(tmp_path: Path) -> None:
    """기존 yaml + proposal → 새 yaml + 백업 + ApplyResult 일관성."""
    current = tmp_path / "daytrade-current.yaml"
    _base_profile().dump_yaml(current)

    prop = DaytradeProposal(
        summary_ko="방어",
        rationale_ko="변동성 ↑",
        confidence=0.7,
        top_n=2,
        stop_loss_pct=0.012,
    )
    result = apply_and_persist(
        prop,
        current_path=current,
        target_path=current,
        today=date(2026, 5, 7),
    )
    assert result.applied_profile.top_n == 2
    assert result.applied_profile.risk.stop_loss_pct == 0.012
    assert result.backup_path is not None
    assert result.backup_path.exists()
    assert result.written_path == current
    # 새 yaml 디스크에서 라운드트립.
    reloaded = DaytradeProfile.load_yaml(current)
    assert reloaded.top_n == 2


def test_apply_and_persist_uses_fallback_when_current_missing(tmp_path: Path) -> None:
    """current 가 없으면 fallback (daytrade-default) 로드."""
    current = tmp_path / "daytrade-current.yaml"  # 미존재
    fallback = tmp_path / "daytrade-default.yaml"
    _base_profile().dump_yaml(fallback)

    prop = DaytradeProposal(summary_ko="x", rationale_ko="y", confidence=0.5, top_n=2)
    result = apply_and_persist(
        prop,
        current_path=current,
        target_path=current,
        fallback_path=fallback,
    )
    assert result.applied_profile.top_n == 2
    assert result.backup_path is None  # 첫 작성이라 백업 없음
    assert current.exists()


def test_format_apply_message_with_changes() -> None:
    prop = DaytradeProposal(
        summary_ko="방어",
        rationale_ko="r",
        confidence=0.7,
        top_n=2,
        themes_to_watch=["반도체"],
    )
    base = _base_profile()
    result = apply_proposal(prop, base)
    msg = format_apply_message(result, prop)
    assert "✅ 적용된 변경" in msg
    assert "top_n: 3 → 2" in msg
    assert "📡 주목 테마: 반도체" in msg


def test_format_apply_message_with_rejection() -> None:
    prop = DaytradeProposal(summary_ko="x", rationale_ko="y", confidence=0.5, top_n=8)
    result = apply_proposal(prop, _base_profile())
    msg = format_apply_message(result, prop)
    assert "⚠️ 거부된 변경" in msg
    assert "top_n 8 거부" in msg


def test_format_apply_message_no_changes() -> None:
    prop = DaytradeProposal(summary_ko="유지", rationale_ko="ok", confidence=0.5)
    result = apply_proposal(prop, _base_profile())
    msg = format_apply_message(result, prop)
    assert "적용된 변경: 없음" in msg


def test_apply_preserves_created_at_and_updates_last_updated_at(tmp_path: Path) -> None:
    base = _base_profile()
    base = base.model_copy(update={"created_at": datetime(2026, 4, 1, tzinfo=KST)})
    prop = DaytradeProposal(summary_ko="x", rationale_ko="y", confidence=0.5, top_n=2)
    out = apply_proposal(prop, base)
    assert out.applied_profile.created_at == datetime(2026, 4, 1, tzinfo=KST)
    assert out.applied_profile.last_updated_at is not None
