"""day-trade 모드 YAML 프로파일.

swing 의 `strategy.profile.StrategyProfile` 와 별개 — day-trade 는 종목을 미리
정하지 않고 매일 자동 발굴이라 구조가 다르다 (universe 자동 / RiskLimits / momentum
게이트 / cooldown 등). 사용자가 토요일 주간 인사이트로 자동 갱신하거나 손으로
편집한다. 24단계에서 LLM 이 같은 스키마로 다음주 프로파일을 자동 작성.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

DaytradeStrategyName = Literal["index_momentum_daytrade", "intraday_momentum"]


class DaytradeStrategyParams(BaseModel):
    """전략 인스턴스에 주입되는 파라미터 (RiskLimits 와 별개)."""

    cooldown_min: int = Field(default=30, ge=0, le=240)
    momentum_lookback_min: int = Field(default=5, ge=1, le=60)
    momentum_threshold_pct: float = Field(default=0.0, ge=0.0, le=10.0)


class DaytradeRegimeRouter(BaseModel):
    """시장국면(KODEX 200 60일봉) 기반 프로파일 자동 라우팅.

    무엇/왜:
    - ``strategy/scanner.classify()`` 가 TREND_UP/DOWN/RANGE/HIGH_VOL 4분류만 해도
      paper-day 가 매일 같은 프로파일을 써왔다. 약세장에선 보수, 박스권에선 짧은 익절이
      유리한데 분기가 없으면 단일 프로파일이 모든 상황에 적용.
    - ``enabled=True`` 면 paper-day 가 시동 시 KODEX 200(069500) 60일봉 → classify →
      ``strategy.regime_router.select_profile_for_regime`` 매핑된 프로파일로 자동 스위치.
      매핑된 파일이 없으면 현재 프로파일 fallback.

    설계:
    - 매핑 규칙은 ``strategy/regime_router.REGIME_PROFILE_MAP`` 의 정적 dict (코드 측).
      YAML 은 on/off 토글만 — 사용자가 매핑을 바꾸려면 새 프로파일 파일 작성으로 표현.
    - ``enabled=False`` (기본) 면 동작 X — 회귀 0.
    - 분기 트리거 실패(KODEX 200 fetch 실패) 시 fail-safe (현재 프로파일 유지 + 경고 로그).
    """

    enabled: bool = False


class DaytradeBreadthGate(BaseModel):
    """시총 200 스냅샷 기반 매수 게이트 (Phase 1).

    무엇/왜:
    - `data/snapshots/topcap-*.jsonl` 마지막 라운드의 상승 종목 비율(advancing_pct)
      + 평균 등락률(mean_change_pct) 두 신호로 시장 강세/약세 판단.
    - 약세면 매수 자체 차단 — 회전율·비용 직접 감소 (사용자 회전율 1순위 정신).
    - `enabled=False` (기본) 면 동작 X — 회귀 0.

    임계는 사용자가 YAML 에서 조정. 데이터 부족(파일 없음) 시 fail-open (게이트 통과).
    """

    enabled: bool = False
    min_advancing_pct: float = Field(default=0.5, ge=0.0, le=1.0)
    min_mean_change_pct: float = Field(default=0.0, ge=-10.0, le=10.0)
    snapshot_dir: str = "data/snapshots"


class DaytradeRiskLimits(BaseModel):
    """RiskLimits 중 day-trade 가 쓰는 부분 + Phase B/C 신규 한도. 보수적 기본."""

    stop_loss_pct: float = Field(default=0.0, ge=0.0, le=0.10)
    trailing_stop_pct: float = Field(default=0.0, ge=0.0, le=0.10)
    partial_take_pct: float = Field(default=0.0, ge=0.0, le=0.10)
    partial_take_qty_ratio: float = Field(default=0.5, ge=0.0, le=1.0)
    max_order_value: float = Field(default=0.0, ge=0.0)
    daily_notional_limit: float = Field(default=0.0, ge=0.0)
    daily_loss_kill: bool = False
    # 29단계 PR9/10 (#14) — 연속 손절 자동 일시중지. 0 이면 비활성.
    max_consecutive_losses: int = Field(default=0, ge=0, le=20)
    consecutive_loss_pause_sec: int = Field(default=86_400, ge=60, le=604_800)
    # 29단계 PR27/28 (#15) — 보유 종목 페어 상관 가드 (0.7 권장). 0 이면 비활성.
    max_position_correlation: float = Field(default=0.0, ge=0.0, le=1.0)
    # 회전율 가드 — 종목당 하루 BUY 허용 횟수. 0 이면 비활성(무제한).
    # 같은 종목 반복 진입으로 비용만 누적되는 패턴을 막는다. SELL/청산은 영향 없음.
    max_buys_per_symbol: int = Field(default=0, ge=0, le=20)
    # churn 브레이크 — 그날 누적 실현손실(원, NET=비용 차감)이 이 값 이상이면 신규 BUY 차단.
    # 0 이면 비활성. 손익 기준이라 잘 번 날은 안 막고 손실 쌓인 날만 막는다. SELL 불변.
    daily_realized_loss_limit: float = Field(default=0.0, ge=0.0)


class DaytradeProfile(BaseModel):
    """day-trade 한 사이클의 풀 설정.

    YAML 한 파일이 한 프로파일. 24단계에서 LLM 이 토요일 cron 으로 자동 갱신할
    `strategies/profiles/daytrade-current.yaml` 의 스키마.
    """

    name: str = Field(min_length=1)
    strategy: DaytradeStrategyName = "index_momentum_daytrade"
    top_n: int = Field(default=3, ge=1, le=10)
    exclude_etf: bool = True
    liquidate: str = Field(default="15:15", description="HH:MM KST")
    discover_delay_sec: int = Field(default=30, ge=0, le=600)
    params: DaytradeStrategyParams = Field(default_factory=DaytradeStrategyParams)
    risk: DaytradeRiskLimits = Field(default_factory=DaytradeRiskLimits)
    breadth_gate: DaytradeBreadthGate = Field(default_factory=DaytradeBreadthGate)
    # 시장국면(KODEX 200) 자동 라우팅. enabled=True 면 paper-day 가 시동 시
    # daytrade-trend / defensive / range / crash 중 하나로 자동 스위치.
    regime_router: DaytradeRegimeRouter = Field(default_factory=DaytradeRegimeRouter)
    # 발굴 회피 종목 코드 목록. 거래량 상위에 떠도 매수 후보에서 즉시 제외.
    # 주간 LLM 리뷰가 "⛔ 회피 종목" 으로 제시한 종목을 다음 주에 자동 반영.
    avoid_symbols: list[str] = Field(default_factory=list)
    notes: str = ""
    created_at: datetime | None = None
    last_updated_at: datetime | None = None

    @field_validator("liquidate")
    @classmethod
    def _validate_hhmm(cls, v: str) -> str:
        try:
            h, m = v.split(":")
            hh, mm = int(h), int(m)
        except ValueError as exc:
            raise ValueError(f"liquidate 는 'HH:MM' 형식이어야 합니다: {v!r}") from exc
        if not (0 <= hh < 24 and 0 <= mm < 60):
            raise ValueError(f"liquidate 시간 범위 벗어남: {v!r}")
        return v

    @classmethod
    def load_yaml(cls, path: Path) -> DaytradeProfile:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"YAML 최상위는 매핑이어야 합니다: {path}")
        return cls.model_validate(raw)

    def dump_yaml(self, path: Path) -> None:
        """한국어 헤더 주석 + body. 사용자가 숫자만 손봐도 되도록 안내."""
        path.parent.mkdir(parents=True, exist_ok=True)
        body = self.model_dump(mode="json", exclude_none=True)
        header = (
            "# day-trade 모드 자동 프로파일 — 숫자만 살짝 바꿔도 됩니다.\n"
            "# 실행: uv run auto-trade paper-day --profile <이 파일 경로> --dry-run\n"
            "# strategy: index_momentum_daytrade (다회 진입 + 모멘텀 게이트) /\n"
            "#           intraday_momentum (첫 봉 1회 진입, 21단계 호환).\n"
            "# risk: 모두 0/false 가 비활성. stop_loss_pct=0.01 → 매수가 -1% 손절.\n"
        )
        text = yaml.safe_dump(
            body,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
        path.write_text(header + text, encoding="utf-8")


__all__ = [
    "DaytradeBreadthGate",
    "DaytradeProfile",
    "DaytradeRegimeRouter",
    "DaytradeRiskLimits",
    "DaytradeStrategyName",
    "DaytradeStrategyParams",
]
