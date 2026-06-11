"""당일 러너 상태 영속 — 재시작 시 일일 한도 기준 복원.

2026-06-10 제3자 검토 — 장중 -2.5% 손실 후 봇이 죽고 launchd 가 재시동하면
`day_start_equity` 가 재시작 시점 평가액으로 리셋돼, 그날 추가로 -3% 를 더
잃어야 daily_loss_limit 이 발동하는 "한도 희석" 문제. 일일 매수대금 누적
(`today_notional_used`)·종목별 BUY 카운트·churn 누적도 같은 이유로 0 부터
다시 시작한다.

구조 (heartbeat 모듈과 동일 원칙):
- 디스크 JSON 한 파일. 같은 KST 날짜일 때만 복원 — 날짜가 바뀌면 무시(자정 리셋 유지).
- 쓰기 실패는 swallow + 구조화 로그 — 디스크 문제로 트레이딩이 멈추지 않게.
- 외부 API 의존 0.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

__all__ = ["DEFAULT_DAY_STATE_PATH", "DayState", "DayStateStore"]

DEFAULT_DAY_STATE_PATH = Path("data/state/day-state.json")


@dataclass
class DayState:
    """하루치 한도 판정 기준값 묶음. 전부 러너가 메모리에 들고 있던 값."""

    date: str  # KST YYYY-MM-DD — 이 날짜가 오늘과 다르면 복원하지 않음
    day_start_equity: float = 0.0
    today_notional_used: float = 0.0
    daily_buy_counts: dict[str, int] = field(default_factory=dict)
    daily_realized_net: float = 0.0


@dataclass
class DayStateStore:
    path: Path = DEFAULT_DAY_STATE_PATH

    def load(self, today: date) -> DayState | None:
        """저장분이 *오늘* 날짜일 때만 DayState 반환. 없음/손상/다른 날짜 → None."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("day_state.load_failed", path=str(self.path), error=str(exc))
            return None
        if raw.get("date") != today.isoformat():
            return None
        try:
            return DayState(
                date=str(raw["date"]),
                day_start_equity=float(raw.get("day_start_equity") or 0.0),
                today_notional_used=float(raw.get("today_notional_used") or 0.0),
                daily_buy_counts={
                    str(k): int(v) for k, v in (raw.get("daily_buy_counts") or {}).items()
                },
                daily_realized_net=float(raw.get("daily_realized_net") or 0.0),
            )
        except (TypeError, ValueError) as exc:
            log.warning("day_state.parse_failed", path=str(self.path), error=str(exc))
            return None

    def save(self, state: DayState) -> None:
        """atomic write (tmp → rename). 실패는 swallow — 주문 경로를 막지 않는다."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(state), ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            log.error("day_state.save_failed", path=str(self.path), error=str(exc))
