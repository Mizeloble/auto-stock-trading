"""신호→주문 latency 모니터링.

29단계 PR19 (#20) — 시그널 발생부터 키움 응답까지 소요 시간을 누적. 실거래에서
백테스트와 PnL 차이의 큰 원인이 latency. p99 가 임계 초과 시 preflight / 별도
cron 이 알림.

구조 — `llm/cost_tracker.py` 와 동일 패턴:
- JSONL append-only (동시성 안전, 손상 라인 스킵).
- 일별 통계 — count / mean / p50 / p95 / p99.
- 디스크 쓰기 실패는 swallow + 로그 (트레이딩 핫패스 보호).

이번 PR 의 책임은 *모듈 + Settings + runner wiring + CLI*. preflight 게이트
승격은 사용자가 p99 임계 결정 후 별도 PR.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import structlog

from .._common import KST
from ..config import Settings, get_settings

log = structlog.get_logger(__name__)

__all__ = [
    "DEFAULT_P99_ALERT_MS",
    "DailyLatencyStats",
    "LatencyEntry",
    "LatencyTracker",
    "get_default_latency_tracker",
]

# 실거래 운영 임계 — 키움 API 1회/3.6초 제한 고려. p99 3초 넘으면 단기 전략 신호 의미 X.
DEFAULT_P99_ALERT_MS = 3000


@dataclass(frozen=True)
class LatencyEntry:
    """단일 측정. JSONL 한 줄과 1:1."""

    ts: str  # ISO 8601 KST
    label: str  # 측정 구간 라벨 (예: "order_send")
    duration_ms: float


@dataclass(frozen=True)
class DailyLatencyStats:
    """일별 통계. 카테고리(label) 별."""

    date: str  # YYYY-MM-DD
    label: str
    count: int
    mean_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float


def _percentile(values: list[float], pct: float) -> float:
    """선형 보간 percentile. 빈 입력은 0."""
    if not values:
        return 0.0
    sorted_v = sorted(values)
    if len(sorted_v) == 1:
        return sorted_v[0]
    rank = (pct / 100.0) * (len(sorted_v) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return sorted_v[lo]
    frac = rank - lo
    return sorted_v[lo] + (sorted_v[hi] - sorted_v[lo]) * frac


@dataclass
class LatencyTracker:
    """JSONL append + 일별 통계.

    파일 경로는 운영에서 ``data/latency.jsonl`` 권장.
    """

    path: Path

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        *,
        label: str,
        duration_ms: float,
        ts: datetime | None = None,
    ) -> LatencyEntry:
        """단일 측정 기록 + JSONL append."""
        entry = LatencyEntry(
            ts=(ts or datetime.now(tz=KST)).isoformat(),
            label=label,
            duration_ms=float(duration_ms),
        )
        line = (
            json.dumps({"ts": entry.ts, "label": entry.label, "duration_ms": entry.duration_ms})
            + "\n"
        )
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError as exc:
            log.error("latency.write_failed", path=str(self.path), error=str(exc))
        return entry

    def replay(self) -> Iterator[LatencyEntry]:
        """저널 전체를 한 줄씩 yield. 손상 라인 스킵."""
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as fh:
            for line_no, raw in enumerate(fh, start=1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    yield LatencyEntry(
                        ts=str(data["ts"]),
                        label=str(data["label"]),
                        duration_ms=float(data["duration_ms"]),
                    )
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                    log.warning(
                        "latency.corrupt_line",
                        path=str(self.path),
                        line_no=line_no,
                        error=str(exc),
                    )

    def daily_stats(self, date_str: str) -> list[DailyLatencyStats]:
        """``YYYY-MM-DD`` 일자의 label 별 통계 리스트. 카테고리 없으면 빈 리스트."""
        by_label: dict[str, list[float]] = {}
        for entry in self.replay():
            if not entry.ts.startswith(date_str):
                continue
            by_label.setdefault(entry.label, []).append(entry.duration_ms)

        results: list[DailyLatencyStats] = []
        for label, values in by_label.items():
            results.append(
                DailyLatencyStats(
                    date=date_str,
                    label=label,
                    count=len(values),
                    mean_ms=round(sum(values) / len(values), 2),
                    p50_ms=round(_percentile(values, 50), 2),
                    p95_ms=round(_percentile(values, 95), 2),
                    p99_ms=round(_percentile(values, 99), 2),
                    max_ms=round(max(values), 2),
                )
            )
        results.sort(key=lambda s: s.label)
        return results


def get_default_latency_tracker(settings: Settings | None = None) -> LatencyTracker | None:
    """``Settings.latency_log_path`` 기반 default 트래커. 미설정 시 None (비활성)."""
    s = settings or get_settings()
    if s.latency_log_path is None:
        return None
    return LatencyTracker(path=s.latency_log_path)
