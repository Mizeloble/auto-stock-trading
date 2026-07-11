"""러너 종료 시 자동으로 일일 리포트를 한 번 푸시하는 Notifier 데코레이터.

흐름:
1. 러너가 finally 에서 `notify("runner.stopped", reason=…)` 호출
2. 이 wrapper 가 inner notifier 로 stopped 를 먼저 보냄
3. 같은 wrapper 가 LogEvents + state snapshot 으로 DailyReport 빌드 + JSON 저장
4. inner notifier 로 `runner.daily_report` 푸시 — 사용자 폰에 한국어 요약 도착

설계 원칙:
- 알림 실패가 트레이딩을 막지 않게 모든 단계 swallow + log.
- 한 번만 발사 — 같은 인스턴스가 stopped 두 번 받아도 중복 안 보냄.
- LLM 호출 X — 본 모듈은 템플릿 리포트만. LLM 인사이트는 22단계 Phase B 가 별도.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from datetime import date as DateT
from pathlib import Path
from typing import Any

import structlog

from ..reports.daily import DailyReport, build_daily_report, save_daily_report
from ..reports.log_parser import parse_log
from .base import Notifier
from .state import RunnerStateSnapshot

log = structlog.get_logger(__name__)


StateProvider = Callable[[], RunnerStateSnapshot]


class ReportingNotifier:
    """inner Notifier 를 감싸 `runner.stopped` 후 일일 리포트를 한 번 푸시.

    log_path / report_root 는 호출자가 결정 — 운영(`logs/paper-day-YYYY-MM-DD.log`,
    `data/reports/`) 과 테스트(임시 디렉터리) 둘 다 자연스럽게 지원.
    """

    def __init__(
        self,
        inner: Notifier,
        *,
        state_provider: StateProvider,
        log_path: Path | str,
        report_root: Path | str = "data/reports",
        report_date: DateT | None = None,
    ) -> None:
        self._inner = inner
        self._state_provider = state_provider
        self._log_path = Path(log_path)
        self._report_root = Path(report_root)
        self._report_date = report_date or DateT.today()
        self._fired: bool = False

    async def notify(self, event: str, **fields: object) -> None:
        # 항상 inner 먼저 — 사용자 알림은 그대로 전달.
        await self._inner.notify(event, **fields)
        if event == "runner.stopped" and not self._fired:
            self._fired = True
            await self._dispatch_daily_report()

    async def aclose(self) -> None:
        await self._inner.aclose()

    # ---- 내부 ----------------------------------------------------------

    async def _dispatch_daily_report(self) -> None:
        try:
            snapshot = self._safe_snapshot()
            events = parse_log(self._log_path)
            report = build_daily_report(
                date=self._report_date.isoformat(),
                events=events,
                snapshot=snapshot,
            )
            # day-trade 모드 + (발굴 또는 거래) 가 있을 때만 푸시. 빈 리포트는 노이즈.
            if not _has_meaningful_content(report):
                log.info("reports.daily.skipped", reason="empty_report")
                return
            # 섀도우 세션(게이트 차단일 가상 매매)은 shadow-*.json 으로 분리 저장 —
            # daily-*.json 소비자(주간 인사이트 등)가 가상 PnL 을 실적으로 오인 방지.
            shadow = bool(snapshot.shadow) if snapshot is not None else False
            try:
                save_path = save_daily_report(
                    report,
                    root=self._report_root,
                    prefix="shadow" if shadow else "daily",
                )
                log.info("reports.daily.saved", path=str(save_path), shadow=shadow)
            except OSError as exc:
                log.warning("reports.daily.save_failed", error=str(exc))
            payload = _to_notify_payload(report)
            if shadow:
                payload["shadow"] = True
            await self._inner.notify("runner.daily_report", **payload)
        except Exception as exc:
            log.warning(
                "reports.daily.dispatch_failed",
                error_type=type(exc).__name__,
                error=str(exc),
            )

    def _safe_snapshot(self) -> RunnerStateSnapshot | None:
        try:
            return self._state_provider()
        except Exception as exc:
            log.warning("reports.daily.snapshot_failed", error=str(exc))
            return None


def _has_meaningful_content(report: DailyReport) -> bool:
    """발굴이나 거래가 하나라도 있어야 푸시 — 그 외엔 노이즈로 보고 skip."""
    return bool(
        report.discovery_chosen
        or report.discovery_blocked
        or report.buys
        or report.liquidations
        or report.error_count
    )


def _to_notify_payload(report: DailyReport) -> dict[str, Any]:
    """telegram._format_message 에 넘길 dict. 직렬화 가능 형태로 정리."""
    payload = asdict(report)
    # tuple → list (notifier kwargs 컨벤션)
    payload["discovery_blocked"] = [list(t) for t in report.discovery_blocked]
    return payload


__all__ = ["ReportingNotifier"]
