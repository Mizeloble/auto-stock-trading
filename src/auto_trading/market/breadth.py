"""시총 200 스냅샷 기반 시장 폭(breadth) 게이트.

매 10분 누적되는 `data/snapshots/topcap-YYYY-MM-DD.jsonl` 의 *가장 최근 라운드*
(같은 ts 200줄)를 읽어 시장 강세/약세 판단:
- **advancing_pct**: 200종목 중 change_pct > 0 비율 (시장 폭 폭넓이)
- **mean_change_pct**: 200종목 change_pct 단순평균

설계:
- market/ 규칙: 읽기 전용. 디스크 읽기만 하고 네트워크 호출 X.
- 한 라운드는 같은 ts → 마지막 라인부터 거꾸로 같은 ts 인 라인까지 모음.
- 파일이 없거나 라운드가 비어있으면 `Breadth.unavailable` — 게이트 통과 처리 (기존 흐름 보존).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import structlog

from .._common import KST
from ..monitor.price_snapshot import snapshot_path_for

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Breadth:
    """한 라운드의 시장 폭 측정.

    `unavailable=True` 면 데이터 부족(파일 없음/라운드 0) — 호출자가 fail-open 결정.
    """

    ts: str  # 마지막 라운드 ts (ISO 8601 KST)
    sample_size: int  # 그 라운드에 들어온 종목 수
    advancing_pct: float  # 0.0 ~ 1.0 (상승 종목 비율)
    declining_pct: float  # 0.0 ~ 1.0 (하락 종목 비율)
    mean_change_pct: float  # change_pct 단순평균
    unavailable: bool = False

    @classmethod
    def unavailable_instance(cls, reason: str) -> Breadth:
        log.warning("market.breadth.unavailable", reason=reason)
        return cls(
            ts="",
            sample_size=0,
            advancing_pct=0.0,
            declining_pct=0.0,
            mean_change_pct=0.0,
            unavailable=True,
        )


@dataclass(frozen=True)
class BreadthGateResult:
    """게이트 판정 결과 + 사용자 로그용 reason."""

    passed: bool
    breadth: Breadth
    reason: str  # "ok" / "advancing_pct_below" / "mean_change_pct_below" / "no_data"


def _load_latest_round(path: Path) -> list[dict]:
    """JSONL 파일 끝에서 같은 ts 인 라인만 추출 (마지막 라운드).

    파일 안 읽힘/JSON 손상은 빈 리스트로 폴백.
    """
    if not path.exists():
        return []
    try:
        # 200 종목 라운드 + 약간 여유. 너무 크게 잡지 말 것 (파일이 커도 마지막만 본다).
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("market.breadth.read_failed", path=str(path), error=str(exc))
        return []

    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []

    # 마지막 라인의 ts 를 기준으로 같은 ts 인 라인만 추출 (역방향).
    parsed: list[dict] = []
    last_ts: str | None = None
    for raw in reversed(lines):
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        ts = row.get("ts")
        if last_ts is None:
            last_ts = ts
        if ts != last_ts:
            break
        parsed.append(row)
    return parsed


def compute_breadth(
    out_dir: Path,
    *,
    when: date | None = None,
) -> Breadth:
    """`out_dir/topcap-YYYY-MM-DD.jsonl` 의 마지막 라운드 → Breadth.

    `when` 미지정 시 오늘. 오늘 파일이 없으면 unavailable 반환.
    """
    target_day = when or datetime.now(tz=KST).date()
    path = snapshot_path_for(out_dir, when=target_day)
    rows = _load_latest_round(path)
    if not rows:
        return Breadth.unavailable_instance(f"no_round_in:{path.name}")

    total = len(rows)
    up = sum(1 for r in rows if float(r.get("change_pct", 0) or 0) > 0)
    down = sum(1 for r in rows if float(r.get("change_pct", 0) or 0) < 0)
    mean = sum(float(r.get("change_pct", 0) or 0) for r in rows) / total
    ts = str(rows[0].get("ts", ""))

    breadth = Breadth(
        ts=ts,
        sample_size=total,
        advancing_pct=up / total,
        declining_pct=down / total,
        mean_change_pct=round(mean, 4),
    )
    log.info(
        "market.breadth.computed",
        ts=ts,
        sample=total,
        advancing_pct=round(breadth.advancing_pct, 3),
        mean_change_pct=breadth.mean_change_pct,
    )
    return breadth


def evaluate_breadth_gate(
    breadth: Breadth,
    *,
    min_advancing_pct: float,
    min_mean_change_pct: float,
) -> BreadthGateResult:
    """게이트 판정.

    - 데이터 부족(unavailable) → fail-open (passed=True, reason="no_data") — 기존 흐름 보존.
    - advancing_pct, mean_change_pct **둘 다** 임계 이상이면 통과 (보수적 AND).
    """
    if breadth.unavailable:
        return BreadthGateResult(passed=True, breadth=breadth, reason="no_data")

    if breadth.advancing_pct < min_advancing_pct:
        return BreadthGateResult(passed=False, breadth=breadth, reason="advancing_pct_below")
    if breadth.mean_change_pct < min_mean_change_pct:
        return BreadthGateResult(passed=False, breadth=breadth, reason="mean_change_pct_below")
    return BreadthGateResult(passed=True, breadth=breadth, reason="ok")


__all__ = [
    "Breadth",
    "BreadthGateResult",
    "compute_breadth",
    "evaluate_breadth_gate",
]
