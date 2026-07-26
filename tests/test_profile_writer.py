"""24단계 Phase C — apply_proposal + write_with_backup + apply_and_persist 단위."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from auto_trading._common import KST
from auto_trading.daytrade.profile import (
    DaytradeProfile,
    DaytradeRegimeRouter,
    DaytradeRiskLimits,
    DaytradeStrategyParams,
)
from auto_trading.news.profile_writer import (
    ApplyResult,
    SafetyCaps,
    apply_and_persist,
    apply_proposal,
    format_apply_message,
    format_inputs_block,
    write_with_backup,
)
from auto_trading.news.proposal import DaytradeProposal
from auto_trading.reports.daily import DailyReport
from auto_trading.strategy.scanner import MarketRegime, RegimeReport


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


def test_apply_proposal_preserves_regime_router_block() -> None:
    """LLM 주간 자동화는 regime_router 를 절대 건드리지 못한다 (화이트리스트 제외).

    무엇/왜: regime_router(시장국면 자동 라우팅)는 사용자가 직접 켜고 끄는 운영 스위치다.
    DaytradeProposal 스키마에 regime_router 필드가 아예 없고, apply_proposal 은
    current.model_dump() 로 시작해 _PROPOSAL_FIELDS_* 화이트리스트만 덮어쓰므로,
    사용자가 켜둔 regime_router.enabled=True 가 자동 갱신으로 꺼지거나 켜지지 않아야 한다.
    (과거 운영 yaml 을 단정하던 깨지기 쉬운 통합 테스트를 이 결정론적 단위로 대체.)
    """
    # 사용자가 regime_router 를 켜둔 프로파일.
    base = _base_profile()
    base = base.model_copy(update={"regime_router": DaytradeRegimeRouter(enabled=True)})
    assert base.regime_router.enabled is True

    # risk·params·top_n·avoid 를 모두 바꾸는 적극적인 proposal.
    prop = DaytradeProposal(
        summary_ko="대폭 변경",
        rationale_ko="ok",
        confidence=0.9,
        top_n=2,
        cooldown_min=90,
        stop_loss_pct=0.02,
        symbols_to_avoid=["005930"],
    )
    out = apply_proposal(prop, base)

    # 다른 필드는 반영됐어도 regime_router 는 사용자 설정 그대로 보존.
    assert out.applied_profile.regime_router.enabled is True
    # 반대로 꺼둔 경우도 자동으로 켜지지 않는다.
    base_off = _base_profile().model_copy(
        update={"regime_router": DaytradeRegimeRouter(enabled=False)}
    )
    out_off = apply_proposal(prop, base_off)
    assert out_off.applied_profile.regime_router.enabled is False


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


def test_apply_avoid_symbols_recorded() -> None:
    """symbols_to_avoid 가 YAML 의 avoid_symbols 로 그대로 반영."""
    prop = DaytradeProposal(
        summary_ko="x",
        confidence=0.5,
        symbols_to_avoid=["356680", "900300"],
    )
    out = apply_proposal(prop, _base_profile())
    assert out.applied_profile.avoid_symbols == ["356680", "900300"]
    assert any("avoid_symbols" in line for line in out.accepted)


def test_apply_avoid_symbols_no_change_when_same() -> None:
    """현재값과 동일하면 accepted 에 들어가지 않음."""
    base = _base_profile()
    base = base.model_copy(update={"avoid_symbols": ["356680"]})
    prop = DaytradeProposal(summary_ko="x", confidence=0.5, symbols_to_avoid=["356680"])
    out = apply_proposal(prop, base)
    assert out.applied_profile.avoid_symbols == ["356680"]
    assert all("avoid_symbols" not in line for line in out.accepted)


def test_apply_max_buys_above_cap_rejected() -> None:
    """SafetyCaps.max_buys_per_symbol_max 보다 큰 제안은 거부 (1차 방어선)."""
    caps = SafetyCaps(max_buys_per_symbol_max=2)
    prop = DaytradeProposal(summary_ko="x", confidence=0.5, max_buys_per_symbol=5)
    out = apply_proposal(prop, _base_profile(), caps=caps)
    assert out.applied_profile.risk.max_buys_per_symbol == 0  # 기본 유지
    assert any("max_buys_per_symbol" in line and "거부" in line for line in out.rejected)


def test_apply_max_buys_within_cap_recorded() -> None:
    """max_buys_per_symbol 정상 범위는 적용."""
    prop = DaytradeProposal(summary_ko="x", confidence=0.5, max_buys_per_symbol=2)
    out = apply_proposal(prop, _base_profile())
    assert out.applied_profile.risk.max_buys_per_symbol == 2
    assert any("max_buys_per_symbol" in line and "→ 2" in line for line in out.accepted)


def test_apply_zero_disable_rejected_for_active_guards() -> None:
    """활성 가드(>0)를 0(비활성)으로 끄는 제안은 캡 이내여도 거부.

    0=기능 끄기 규약 — stop_loss_pct=0 이면 손절이 아예 사라지므로
    상한 캡만으로는 못 막는 방향. 끄는 건 사람이 YAML 직접 수정으로만.
    """
    prop = DaytradeProposal(
        summary_ko="x",
        rationale_ko="y",
        confidence=0.5,
        stop_loss_pct=0.0,  # 활성 0.01 → 끄기: 거부
        cooldown_min=0,  # 활성 30 → 끄기: 거부
    )
    out = apply_proposal(prop, _base_profile())
    assert out.applied_profile.risk.stop_loss_pct == 0.01  # 변경 X
    assert out.applied_profile.params.cooldown_min == 30  # 변경 X
    assert any("stop_loss_pct 0.0 거부" in line for line in out.rejected)
    assert any("cooldown_min 0 거부" in line for line in out.rejected)


def test_apply_zero_ok_when_guard_already_inactive() -> None:
    """이미 0(비활성)인 필드에 0 제안 — 변경도 거부도 아님 (노이즈 방지)."""
    prop = DaytradeProposal(
        summary_ko="x", rationale_ko="y", confidence=0.5, momentum_threshold_pct=0.0
    )
    out = apply_proposal(prop, _base_profile())  # base 의 momentum_threshold_pct=0.0
    assert out.accepted == []
    assert out.rejected == []


def test_apply_max_buys_zero_disable_rejected_when_active() -> None:
    """max_buys_per_symbol 은 0=무제한 — 활성(2) → 0 제안은 회전율 가드 해제라 거부."""
    base = _base_profile()
    base = base.model_copy(update={"risk": base.risk.model_copy(update={"max_buys_per_symbol": 2})})
    prop = DaytradeProposal(summary_ko="x", confidence=0.5, max_buys_per_symbol=0)
    out = apply_proposal(prop, base)
    assert out.applied_profile.risk.max_buys_per_symbol == 2  # 변경 X
    assert any("max_buys_per_symbol 0 거부" in line for line in out.rejected)


def test_apply_daily_loss_kill_disable_rejected() -> None:
    """daily_loss_kill True → False (안전 가드 해제) 제안은 거부. False → True 는 허용 유지."""
    base = _base_profile()
    base = base.model_copy(update={"risk": base.risk.model_copy(update={"daily_loss_kill": True})})
    prop = DaytradeProposal(summary_ko="x", confidence=0.5, daily_loss_kill=False)
    out = apply_proposal(prop, base)
    assert out.applied_profile.risk.daily_loss_kill is True  # 변경 X
    assert any("daily_loss_kill False 거부" in line for line in out.rejected)


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
    # 새 형식: "• 동시 보유 종목 수 (top_n): 3 → 2" + 풀이 라인.
    assert "동시 보유 종목 수" in msg
    assert "(top_n)" in msg
    assert "3 → 2" in msg
    assert "👍" in msg  # 글로서리 풀이가 메시지에 들어갔다는 표시
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


# ---- format_inputs_block (정량 입력 블록) ---------------------------------


def _stub_report(
    iso_date: str,
    *,
    buys: int = 0,
    liqs: int = 0,
    errors: int = 0,
    realized_after_cost: float | None = None,
    turnover: float | None = None,
) -> DailyReport:
    """format_inputs_block 만 호출하므로 표시에 안 쓰이는 필드는 안전한 기본값."""
    return DailyReport(
        date=iso_date,
        mode="day-trade",
        dry_run=False,
        discovery_chosen=[],
        discovery_blocked=[],
        market_gates=[],
        buys=[None] * buys,  # type: ignore[list-item]
        liquidations=[None] * liqs,  # type: ignore[list-item]
        error_count=errors,
        note_ko="",
        realized_pnl_after_cost=realized_after_cost,
        turnover_ratio=turnover,
    )


def test_format_inputs_block_with_reports_and_regime() -> None:
    reports = [
        _stub_report(
            "2026-05-18", buys=3, liqs=0, errors=1, realized_after_cost=20000.0, turnover=0.04
        ),
        _stub_report(
            "2026-05-22", buys=5, liqs=1, errors=2, realized_after_cost=60000.0, turnover=0.07
        ),
    ]
    regime = RegimeReport(
        regime=MarketRegime.HIGH_VOL,
        metrics={"atr_pct": 0.0427, "bb_width_pct": 0.336},
        summary_ko="고변동성 (ATR 4.27% / 봉) — 추세·박스 신호가 휩쓸릴 위험.",
    )
    out = format_inputs_block(reports=reports, news_total=190, news_to_llm=80, regime=regime)
    assert "📊 이번 결정의 입력" in out
    assert "2026-05-18 ~ 2026-05-22 (2일)" in out
    assert "매수 8건 / 청산 1건 / 에러 3건" in out
    assert "실현손익 표본 2일" in out
    assert "+40,000원" in out  # (20000 + 60000) / 2
    assert "평균 회전율 5.5%" in out  # (0.04 + 0.07) / 2 * 100
    assert "high_vol" in out
    assert "수집 190건 → LLM 에 80건" in out


def test_format_inputs_block_no_reports_no_regime() -> None:
    out = format_inputs_block(reports=[], news_total=0, news_to_llm=0, regime=None)
    assert "일일 리포트 0건 — 데이터 부족" in out
    assert "시장국면: 정보 없음" in out
    assert "입력 뉴스: 수집 0건" in out


def test_format_inputs_block_reports_without_pnl_or_turnover() -> None:
    reports = [_stub_report("2026-05-22", buys=1)]
    out = format_inputs_block(reports=reports, news_total=10, news_to_llm=10, regime=None)
    assert "매수 1건" in out
    # 손익·회전율 데이터 없을 때 해당 라인은 생략 (메시지 깨지지 않게).
    assert "실현손익" not in out
    assert "회전율" not in out


# ---- format_apply_message — 변경별 이유 + 4섹션 근거 -------------------


def _proposal_with_changes_and_reasons() -> DaytradeProposal:
    return DaytradeProposal(
        summary_ko="고변동성 대응 + 회전율 축소",
        confidence=0.6,
        signals_ko=[
            "지난 7일 매수 18건 중 13건이 1.2~1.5% 구간에서 손절",
            "외국인 순매도 4일 누적, KOSPI 5일 변동성 +28%",
        ],
        judgment_ko="ATR 4.27% 환경에서 1.2% 손절은 노이즈와 겹침. 1.8% 가 안전 마진.",
        referenced_headlines_ko=[
            "원·달러 1,500원 고착 — 외환 변동성 장기화",
            "외국인, 코스피 4일 연속 순매도",
        ],
        weekly_memo_ko="FOMC 결과에 따라 daily_loss_kill 발동 가능성, 모니터링 권장.",
        top_n=4,
        stop_loss_pct=0.018,
        change_reasons={
            "stop_loss_pct": "손절 13건 중 9건이 노이즈 발동(재진입 후 +0.8% 반등). 1.8% 로 ATR 안전 마진 확보.",
            "top_n": "회전율 축소 위해 한 종목 비중 ↓, 분산 ↑.",
        },
    )


def test_format_apply_message_renders_change_reasons_under_each_change() -> None:
    proposal = _proposal_with_changes_and_reasons()
    result = ApplyResult(
        applied_profile=_base_profile(),
        accepted=["top_n: 3 → 4", "stop_loss_pct: 0.012 → 0.018"],
    )
    msg = format_apply_message(result, proposal)
    # 변경마다 정의/👍/👎 + 📌 이번에 바꾼 이유 가 함께 표시.
    assert "📌 이번에 바꾼 이유: 회전율 축소 위해" in msg
    assert "📌 이번에 바꾼 이유: 손절 13건 중 9건이 노이즈" in msg


def test_format_apply_message_renders_4section_rationale() -> None:
    proposal = _proposal_with_changes_and_reasons()
    result = ApplyResult(applied_profile=_base_profile(), accepted=["top_n: 3 → 4"])
    msg = format_apply_message(result, proposal)
    assert "📝 근거" in msg
    assert "📍 시그널" in msg
    assert "지난 7일 매수 18건" in msg
    assert "🎯 판단" in msg
    assert "ATR 4.27% 환경" in msg
    assert "🗞 영향 헤드라인" in msg
    assert "원·달러 1,500원 고착" in msg
    assert "📅 다음 주 메모" in msg
    assert "FOMC" in msg


def test_format_apply_message_omits_empty_rationale_sections() -> None:
    """signals_ko/judgment_ko 등 빈 섹션은 헤더가 안 나타나야 메시지가 깔끔."""
    proposal = DaytradeProposal(summary_ko="유지", confidence=0.5)
    result = ApplyResult(applied_profile=_base_profile())
    msg = format_apply_message(result, proposal)
    assert "📍 시그널" not in msg
    assert "🎯 판단" not in msg
    assert "🗞 영향 헤드라인" not in msg
    assert "📅 다음 주 메모" not in msg


def test_format_apply_message_with_inputs_block_inserted() -> None:
    proposal = DaytradeProposal(summary_ko="x", confidence=0.5)
    result = ApplyResult(applied_profile=_base_profile())
    msg = format_apply_message(result, proposal, inputs_block="📊 이번 결정의 입력\n• 매수 8건")
    assert "📊 이번 결정의 입력" in msg
    assert "매수 8건" in msg


def test_format_apply_message_skips_missing_reason() -> None:
    """change_reasons 에 키가 없으면 풀이만 표시되고 ``📌`` 라인은 생략."""
    proposal = DaytradeProposal(summary_ko="x", confidence=0.5, top_n=4, change_reasons={})
    result = ApplyResult(applied_profile=_base_profile(), accepted=["top_n: 3 → 4"])
    msg = format_apply_message(result, proposal)
    assert "(top_n): 3 → 4" in msg
    assert "📌 이번에 바꾼 이유" not in msg
