"""레짐 → daytrade 프로파일 자동 라우팅 — 29단계 PR40 (#5).

무엇/왜:
- ``scanner.classify()`` (PR21 부근) 가 MarketRegime 을 분류해도 paper-day 가
  매번 같은 프로파일을 씀. 추세장 vs 횡보장은 정반대 전략이 유리한데 단일
  프로파일이 모든 상황에 똑같이 작동.
- 이 모듈은 *판정 → 프로파일 경로* 만 책임. 실제 paper-day 분기 적용은 별도 PR.

매핑 (관행):
- TREND_UP   → daytrade-trend.yaml
- TREND_DOWN → daytrade-defensive.yaml (보수적 — 손절 좁게)
- RANGE      → daytrade-range.yaml (박스권 — 짧은 익절·손절)
- HIGH_VOL   → daytrade-highvol.yaml (고변동성 — 소액 능동매매. 완전정지는
  daytrade-crash.yaml 로 수동 전환)

설계 결정:
- 매핑은 정적 dict — 사용자가 새 프로파일 추가 시 ``REGIME_PROFILE_MAP`` 만 수정.
- 매핑된 파일 없으면 ``fallback`` 경로 (보통 daytrade-current.yaml 또는 default).
- 외부 호출 0. 파일 존재 확인 외엔 I/O 없음.

어떻게 확인: tests/test_strategy_regime_router.py.
"""

from __future__ import annotations

from pathlib import Path

from .scanner import MarketRegime

__all__ = [
    "REGIME_PROFILE_MAP",
    "select_profile_for_regime",
]


# regime → 권장 프로파일 파일명 (확장자 제외). 운영자가 새 프로파일 추가 시 갱신.
REGIME_PROFILE_MAP: dict[MarketRegime, str] = {
    MarketRegime.TREND_UP: "daytrade-trend",
    MarketRegime.TREND_DOWN: "daytrade-defensive",
    MarketRegime.RANGE: "daytrade-range",
    # 고변동성: 전면정지(crash) 대신 소액 능동매매(highvol). breadth·지수 게이트가
    # 일별 필터, highvol 프로파일이 사이즈·손절을 변동성장에 맞춰 줄인다. 완전정지가
    # 필요하면 daytrade-crash.yaml 로 수동 전환 (킬스위치로 보존).
    MarketRegime.HIGH_VOL: "daytrade-highvol",
}


def select_profile_for_regime(
    regime: MarketRegime,
    *,
    profiles_root: Path,
    fallback: Path | None = None,
) -> Path | None:
    """레짐에 맞는 프로파일 경로 — 없으면 fallback.

    매개변수:
    - ``profiles_root``: 프로파일 yaml 들이 모여있는 폴더 (예: strategies/profiles/).
    - ``fallback``: 매핑된 파일이 존재하지 않을 때 반환. None 이면 None 반환.

    반환:
    - 매핑된 파일이 존재 → 그 경로.
    - 미존재 + fallback 주어짐 + 그것도 존재 → fallback.
    - 둘 다 없음 → None.
    """
    name = REGIME_PROFILE_MAP.get(regime)
    if name is not None:
        candidate = profiles_root / f"{name}.yaml"
        if candidate.exists():
            return candidate
    if fallback is not None and fallback.exists():
        return fallback
    return None
