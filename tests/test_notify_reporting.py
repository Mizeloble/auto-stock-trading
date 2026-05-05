"""ReportingNotifier — 러너 종료 시 자동 일일 리포트 푸시 + JSON 저장 회귀."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from auto_trading.notify.reporting import ReportingNotifier
from auto_trading.notify.state import RunnerStateSnapshot


class _RecordingNotifier:
    """푸시 이벤트 + 호출 순서 추적용 인메모리 stub."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []
        self.closed: bool = False

    async def notify(self, event: str, **fields: object) -> None:
        self.events.append((event, dict(fields)))

    async def aclose(self) -> None:
        self.closed = True


def _snapshot(*, mode: str = "day-trade", dry_run: bool = True) -> RunnerStateSnapshot:
    return RunnerStateSnapshot(
        mode="mock",
        dry_run=dry_run,
        symbols=[],
        strategies={},
        started_at=None,
        snapshot=None,
        account=None,
        recent_signals={},
        balance_failures=0,
        runner_mode=mode,
    )


def _write_log(path: Path, *json_lines: dict[str, object]) -> Path:
    path.write_text(
        "\n".join(json.dumps({"event": j["event"], **j}) for j in json_lines), encoding="utf-8"
    )
    return path


async def test_passthrough_other_events_do_not_trigger_report(tmp_path: Path) -> None:
    inner = _RecordingNotifier()
    log_path = _write_log(tmp_path / "x.log", {"event": "daytrade.discovery", "chosen": ["A"]})
    wrapped = ReportingNotifier(
        inner,
        state_provider=lambda: _snapshot(),
        log_path=log_path,
        report_root=tmp_path / "reports",
        report_date=date(2026, 5, 5),
    )
    await wrapped.notify("runner.started", strategy="X", symbols="A,B", dry_run=True)
    await wrapped.notify("daytrade.discovery_done", chosen=["A"])
    # daily_report 는 아직 없음 — runner.stopped 가 트리거.
    assert [e for e, _ in inner.events] == ["runner.started", "daytrade.discovery_done"]


async def test_stopped_triggers_report_with_log_data(tmp_path: Path) -> None:
    inner = _RecordingNotifier()
    log_path = _write_log(
        tmp_path / "paper-day-2026-05-05.log",
        {"event": "daytrade.discovery", "chosen": ["018880"], "blocked": []},
        {
            "event": "runner.order_simulated",
            "symbol": "018880",
            "side": "buy",
            "qty": 10,
            "price": 5000.0,
        },
        {"event": "runner.liquidate_simulated", "symbol": "018880", "qty": 10},
    )
    report_root = tmp_path / "reports"
    wrapped = ReportingNotifier(
        inner,
        state_provider=lambda: _snapshot(),
        log_path=log_path,
        report_root=report_root,
        report_date=date(2026, 5, 5),
    )

    await wrapped.notify("runner.stopped", reason="정상 종료")

    # 순서: stopped 먼저, 그 뒤 daily_report.
    names = [e for e, _ in inner.events]
    assert names == ["runner.stopped", "runner.daily_report"]
    _, payload = inner.events[1]
    assert payload["date"] == "2026-05-05"
    assert payload["discovery_chosen"] == ["018880"]
    # JSON 파일 저장 확인.
    saved = report_root / "daily-2026-05-05.json"
    assert saved.exists()
    data = json.loads(saved.read_text(encoding="utf-8"))
    assert data["discovery_chosen"] == ["018880"]


async def test_stopped_only_fires_report_once(tmp_path: Path) -> None:
    """같은 인스턴스가 stopped 두 번 받아도 daily_report 는 한 번만."""
    inner = _RecordingNotifier()
    log_path = _write_log(tmp_path / "x.log", {"event": "daytrade.discovery", "chosen": ["A"]})
    wrapped = ReportingNotifier(
        inner,
        state_provider=lambda: _snapshot(),
        log_path=log_path,
        report_root=tmp_path,
        report_date=date(2026, 5, 5),
    )
    await wrapped.notify("runner.stopped", reason="정상")
    await wrapped.notify("runner.stopped", reason="정상")
    daily_count = sum(1 for e, _ in inner.events if e == "runner.daily_report")
    assert daily_count == 1


async def test_empty_report_skipped(tmp_path: Path) -> None:
    """발굴·거래·에러 다 없으면 daily_report 푸시 안 함 (노이즈 차단)."""
    inner = _RecordingNotifier()
    log_path = tmp_path / "empty.log"
    log_path.write_text("키움 모드: mock\n", encoding="utf-8")
    wrapped = ReportingNotifier(
        inner,
        state_provider=lambda: _snapshot(),
        log_path=log_path,
        report_root=tmp_path,
        report_date=date(2026, 5, 5),
    )
    await wrapped.notify("runner.stopped", reason="정상")
    assert [e for e, _ in inner.events] == ["runner.stopped"]


async def test_aclose_propagates_to_inner(tmp_path: Path) -> None:
    inner = _RecordingNotifier()
    wrapped = ReportingNotifier(
        inner,
        state_provider=lambda: _snapshot(),
        log_path=tmp_path / "missing.log",  # 존재하지 않아도 OK
        report_root=tmp_path,
        report_date=date(2026, 5, 5),
    )
    await wrapped.aclose()
    assert inner.closed is True


async def test_state_provider_failure_does_not_stop_pipeline(tmp_path: Path) -> None:
    """snapshot 호출이 실패해도 events 로만 리포트 빌드 — log 가 의미있으면 푸시."""

    def _bomb() -> RunnerStateSnapshot:
        raise RuntimeError("snapshot failed")

    inner = _RecordingNotifier()
    log_path = _write_log(
        tmp_path / "x.log",
        {"event": "daytrade.discovery", "chosen": ["A"], "blocked": []},
    )
    wrapped = ReportingNotifier(
        inner,
        state_provider=_bomb,
        log_path=log_path,
        report_root=tmp_path,
        report_date=date(2026, 5, 5),
    )
    await wrapped.notify("runner.stopped", reason="정상")
    assert any(e == "runner.daily_report" for e, _ in inner.events)
