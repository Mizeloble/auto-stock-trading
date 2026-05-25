"""glossary.expand_change_with_glossary — accepted 라인 풀이 검증."""

from __future__ import annotations

from auto_trading.news.glossary import expand_change_with_glossary
from auto_trading.news.proposal import DaytradeProposal


def test_glossary_covers_every_proposal_field() -> None:
    """DaytradeProposal 의 변경 가능 필드 9개가 모두 글로서리에 매핑돼 있어야.

    빠진 키가 있으면 텔레그램 메시지에 풀이 없이 원문이 그대로 나가니까,
    필드 추가 시 이 테스트가 1번에 잡는다.
    """
    proposal_fields = {
        "top_n",
        "cooldown_min",
        "momentum_lookback_min",
        "momentum_threshold_pct",
        "stop_loss_pct",
        "trailing_stop_pct",
        "partial_take_pct",
        "partial_take_qty_ratio",
        "daily_loss_kill",
    }
    declared = set(DaytradeProposal.model_fields.keys())
    # 안전망: DaytradeProposal 에 위 9개가 실제 존재해야 함 (필드 rename 감지).
    assert proposal_fields.issubset(declared), (
        f"DaytradeProposal 필드와 불일치: 누락 {proposal_fields - declared}"
    )
    for key in proposal_fields:
        out = expand_change_with_glossary(f"{key}: 0.01 → 0.02")
        assert len(out) >= 2, f"{key} 풀이 누락 — _GLOSSARY 에 항목 추가 필요"


def test_increase_shows_pros_up_and_cons_up() -> None:
    out = expand_change_with_glossary("stop_loss_pct: 0.012 → 0.018")
    # 헤드라인: 라벨 + 키 + 양쪽 % 포맷
    assert out[0] == "• 손절선 (stop_loss_pct): 1.2% → 1.8%"
    # 정의 줄
    assert out[1].startswith("  └ "), out[1]
    # 올림이면 pros_up + cons_up
    joined = "\n".join(out)
    assert "👍 일시 변동에 덜 털림" in joined
    assert "👎 발동되면 손실 절대값이 더 큼" in joined


def test_decrease_shows_pros_down_and_cons_down() -> None:
    out = expand_change_with_glossary("stop_loss_pct: 0.018 → 0.012")
    joined = "\n".join(out)
    assert "1.8% → 1.2%" in joined
    assert "👍 손실을 빨리 끊음" in joined
    assert "👎 노이즈에 자주 손절" in joined


def test_integer_value_not_percent_formatted() -> None:
    """top_n 처럼 is_pct=False 인 키는 정수 그대로."""
    out = expand_change_with_glossary("top_n: 3 → 4")
    assert out[0] == "• 동시 보유 종목 수 (top_n): 3 → 4"
    assert "👍 더 많이 분산" in "\n".join(out)


def test_daily_loss_kill_true_treated_as_up() -> None:
    out = expand_change_with_glossary("daily_loss_kill: False → True")
    joined = "\n".join(out)
    assert "일중 손실 강제 청산" in joined
    assert "👍 큰 손실 차단" in joined
    assert "👎 장중 반등 기회를 차단" in joined


def test_daily_loss_kill_false_treated_as_down() -> None:
    out = expand_change_with_glossary("daily_loss_kill: True → False")
    joined = "\n".join(out)
    assert "👍 장중 반등 기회를 살림" in joined


def test_bullet_prefix_accepted() -> None:
    """호출처가 "• " 접두사를 이미 붙여둔 형태도 같은 결과로."""
    a = expand_change_with_glossary("• cooldown_min: 90 → 60")
    b = expand_change_with_glossary("cooldown_min: 90 → 60")
    assert a == b


def test_unknown_key_passthrough() -> None:
    out = expand_change_with_glossary("mystery_param: 1 → 2")
    assert out == ["mystery_param: 1 → 2"]


def test_unparseable_line_passthrough() -> None:
    out = expand_change_with_glossary("top_n 8 거부 (캡 5)")
    assert out == ["top_n 8 거부 (캡 5)"]


def test_percent_trailing_zero_stripped() -> None:
    """0.020 → "2%" (소수점·trailing 0 제거)."""
    out = expand_change_with_glossary("partial_take_pct: 0.01 → 0.02")
    assert "1% → 2%" in out[0]


def test_percent_keeps_fractional_when_needed() -> None:
    """0.0025 → "0.25%" (필요한 자릿수는 살림)."""
    out = expand_change_with_glossary("momentum_threshold_pct: 0.001 → 0.0025")
    assert "0.1% → 0.25%" in out[0]
