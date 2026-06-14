"""LLM 호출 비용 누적 추적 + 월 한도 알림.

29단계 PR13 (#16 1차) — Anthropic API 호출당 input/output 토큰을 누적해
월 비용을 계산. 한도(예: $5/월) 임계 도달 시 알림 / 차단 결정 근거 제공.

이번 PR 의 책임은 *순수 모듈* (저장 + 합산 + 한도 검사). LLM 호출 지점
(`news/proposal.py` / `notify/insight.py` / `strategy/llm_advisor.py`)
wiring 은 PR14 로 분리.

설계 원칙:
- JSONL append-only — `account/order_journal.py` 와 동일 패턴 (동시성 안전,
  손상 라인 스킵).
- 모델별 가격은 모듈 상수. 가격 변동 시 한 곳만 수정.
- 알 수 없는 모델은 가장 비싼 default 가격 (Opus) — 누락 시 비용을 *과대*
  추정하는 게 안전 (실제는 더 적게 청구되므로 한도 보호).
- 월 경계는 KST 기준 (`YYYY-MM`).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import structlog

from .._common import KST, iter_jsonl

log = structlog.get_logger(__name__)

__all__ = [
    "DEFAULT_BUDGET_USD",
    "MODEL_PRICES_USD_PER_M",
    "BudgetStatus",
    "CostEntry",
    "CostTracker",
    "MonthlySummary",
]

# Anthropic API 가격 (USD per 1M 토큰) — 2026-05 기준. 변동 시 여기만 수정.
# (input, output)
MODEL_PRICES_USD_PER_M: dict[str, tuple[float, float]] = {
    "claude-opus-4-7": (15.0, 75.0),
    "claude-opus-4-6": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
# 알 수 없는 모델은 가장 비싼 가격으로 *과대* 추정 — 한도 보호.
_DEFAULT_PRICE = (15.0, 75.0)

# memory 의 cost discipline — 월 $5 이하.
DEFAULT_BUDGET_USD = 5.0
DEFAULT_ALERT_PCT = 0.8  # 80% 도달 시 알림


@dataclass(frozen=True)
class CostEntry:
    """단일 호출 기록. JSONL 한 줄과 1:1."""

    ts: str  # ISO 8601 KST
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


@dataclass(frozen=True)
class MonthlySummary:
    """한 달치 누적. CostTracker.monthly_summary() 결과."""

    year_month: str  # "2026-05"
    total_usd: float
    call_count: int
    by_model: dict[str, float]


@dataclass(frozen=True)
class BudgetStatus:
    """한도 검사 결과. CLI/preflight/notifier 가 분기 근거로 사용."""

    year_month: str
    spent_usd: float
    budget_usd: float
    pct_used: float
    alert: bool  # >= alert_pct (예: 80%)
    over: bool  # >= 100% — LLM 호출 차단


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """모델 + 토큰 카운트 → USD 비용. 모르는 모델은 default(가장 비싼) 가격."""
    in_price, out_price = MODEL_PRICES_USD_PER_M.get(model, _DEFAULT_PRICE)
    return (input_tokens * in_price + output_tokens * out_price) / 1_000_000.0


def _month_key(ts: datetime | None = None) -> str:
    """KST 기준 월 키 'YYYY-MM'."""
    return (ts or datetime.now(tz=KST)).strftime("%Y-%m")


@dataclass
class CostTracker:
    """JSONL append + 월별 합산 + 한도 검사.

    파일 경로는 운영에서 ``data/llm-cost.jsonl`` 권장. 단위 테스트는 tmp_path.
    """

    path: Path
    monthly_budget_usd: float = DEFAULT_BUDGET_USD
    alert_pct: float = DEFAULT_ALERT_PCT

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
        ts: datetime | None = None,
    ) -> CostEntry:
        """단일 호출 기록 + JSONL append.

        반환된 CostEntry 의 ``cost_usd`` 가 실제 추정 비용. 호출자가 알림
        결정에 즉시 사용 가능.
        """
        entry = CostEntry(
            ts=(ts or datetime.now(tz=KST)).isoformat(),
            model=model,
            input_tokens=int(input_tokens),
            output_tokens=int(output_tokens),
            cost_usd=estimate_cost_usd(model, input_tokens, output_tokens),
        )
        line = json.dumps(entry.__dict__, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError as exc:
            # 디스크 실패가 LLM 호출 성공 자체를 막지 않게 swallow + 로그.
            log.error("llm_cost.write_failed", path=str(self.path), error=str(exc))
        return entry

    def replay(self) -> Iterator[CostEntry]:
        """저널 전체를 한 줄씩 yield. 손상 라인 스킵."""
        for line_no, data in iter_jsonl(self.path, log_event="llm_cost.corrupt_line", logger=log):
            try:
                yield CostEntry(
                    ts=str(data["ts"]),
                    model=str(data["model"]),
                    input_tokens=int(data["input_tokens"]),
                    output_tokens=int(data["output_tokens"]),
                    cost_usd=float(data["cost_usd"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                log.warning(
                    "llm_cost.corrupt_line",
                    path=str(self.path),
                    line_no=line_no,
                    error=str(exc),
                )

    def monthly_summary(self, year_month: str | None = None) -> MonthlySummary:
        """주어진 'YYYY-MM' 월의 누적 (default: 이번 달 KST)."""
        target = year_month or _month_key()
        total = 0.0
        count = 0
        by_model: dict[str, float] = {}
        for entry in self.replay():
            # ts 의 앞 7자가 'YYYY-MM' (ISO 8601 표준 형식 의존).
            if not entry.ts.startswith(target):
                continue
            total += entry.cost_usd
            count += 1
            by_model[entry.model] = by_model.get(entry.model, 0.0) + entry.cost_usd
        return MonthlySummary(
            year_month=target,
            total_usd=round(total, 6),
            call_count=count,
            by_model={k: round(v, 6) for k, v in by_model.items()},
        )

    def budget_status(self, year_month: str | None = None) -> BudgetStatus:
        """한도 대비 현 사용률 + alert/over 분기."""
        summary = self.monthly_summary(year_month)
        budget = max(self.monthly_budget_usd, 1e-9)  # division-by-zero 회피
        pct = summary.total_usd / budget
        return BudgetStatus(
            year_month=summary.year_month,
            spent_usd=summary.total_usd,
            budget_usd=self.monthly_budget_usd,
            pct_used=round(pct, 4),
            alert=pct >= self.alert_pct,
            over=pct >= 1.0,
        )
