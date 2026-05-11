"""데이터 정합성 검사 — daily 리포트 / 백테스트 결과 / 주문 저널.

29단계 PR22 (#19) — 데이터 소스가 깨지면 백테스트가 거짓 양호하게 나오거나
인사이트 LLM 이 잘못된 입력을 본다. 외부 데이터 의존이 늘어날수록 정합성
검사가 가치 큼.

설계 원칙:
- 모든 검사 함수는 *순수 read-only*. 디스크 수정 X.
- 한 파일의 손상이 다른 파일 검사를 막지 않게 isolated.
- 결과는 `IntegrityIssue` 리스트 — 빈 리스트면 OK.
- CLI 는 표 출력 + 손상 1건 이상이면 exit 1.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

__all__ = [
    "IntegrityIssue",
    "check_daily_reports",
    "check_order_journal",
]


@dataclass(frozen=True)
class IntegrityIssue:
    """단일 문제. CLI 가 path/severity/detail 로 표 출력."""

    path: str
    kind: str  # "json_decode" / "missing_field" / "invalid_value" / "stale_data" / "line_corrupt"
    detail: str
    severity: str = "error"  # "error" / "warning"


def check_daily_reports(reports_root: Path) -> list[IntegrityIssue]:
    """`data/reports/daily-*.json` 파일들의 무결성 + 필수 필드 검사.

    검사 항목:
    - JSON 파싱 가능.
    - `date` 필드 존재 + ISO 형식 (YYYY-MM-DD).
    - `realized_pnl_pct` 가 있다면 합리적 범위 (-100 ~ +100, % 단위).
    - 비어있는 파일은 warning.
    """
    issues: list[IntegrityIssue] = []
    if not reports_root.exists() or not reports_root.is_dir():
        return issues  # 폴더 자체가 없으면 검사할 게 없음 — 빈 리스트.

    for path in sorted(reports_root.glob("daily-*.json")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            issues.append(IntegrityIssue(path=str(path), kind="read_error", detail=str(exc)))
            continue
        if not text.strip():
            issues.append(
                IntegrityIssue(
                    path=str(path),
                    kind="empty_file",
                    detail="파일이 비어있음",
                    severity="warning",
                )
            )
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            issues.append(IntegrityIssue(path=str(path), kind="json_decode", detail=str(exc)))
            continue

        if not isinstance(data, dict):
            issues.append(
                IntegrityIssue(
                    path=str(path),
                    kind="missing_field",
                    detail=f"최상위가 dict 아님: {type(data).__name__}",
                )
            )
            continue

        date_val = data.get("date")
        if not isinstance(date_val, str):
            issues.append(
                IntegrityIssue(
                    path=str(path),
                    kind="missing_field",
                    detail="`date` 필드 누락 또는 문자열 아님",
                )
            )
        else:
            # 파일명의 date 와 JSON 안 date 가 일치하는지 (실수 검출).
            stem_date = path.stem.removeprefix("daily-")
            if stem_date != date_val:
                issues.append(
                    IntegrityIssue(
                        path=str(path),
                        kind="invalid_value",
                        detail=f"파일명({stem_date}) 와 date 필드({date_val}) 불일치",
                    )
                )

        pnl_pct = data.get("realized_pnl_pct")
        if pnl_pct is not None:
            try:
                pnl_f = float(pnl_pct)
            except TypeError, ValueError:
                issues.append(
                    IntegrityIssue(
                        path=str(path),
                        kind="invalid_value",
                        detail=f"`realized_pnl_pct` 가 float 아님: {pnl_pct!r}",
                    )
                )
            else:
                if not -100.0 <= pnl_f <= 100.0:
                    issues.append(
                        IntegrityIssue(
                            path=str(path),
                            kind="invalid_value",
                            detail=(
                                f"`realized_pnl_pct={pnl_f}` 가 비정상 — 단위(% vs 비율) "
                                "혼동 또는 데이터 손상"
                            ),
                        )
                    )

    return issues


def check_order_journal(journal_path: Path) -> list[IntegrityIssue]:
    """`data/orders/state.jsonl` 각 라인이 valid JSON + 필수 키 보유 검사.

    검사 항목:
    - 라인 단위 JSON 파싱 가능.
    - 각 라인에 `event` + `client_id` 필드.
    - `event` 값이 알려진 카테고리 (submit/accepted/failed).
    """
    issues: list[IntegrityIssue] = []
    if not journal_path.exists():
        return issues  # 저널 없음 = 봇 한 번도 안 돈 상태 — 정상.

    valid_events = {"submit", "accepted", "failed"}
    try:
        text = journal_path.read_text(encoding="utf-8")
    except OSError as exc:
        return [IntegrityIssue(path=str(journal_path), kind="read_error", detail=str(exc))]

    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            issues.append(
                IntegrityIssue(
                    path=str(journal_path),
                    kind="line_corrupt",
                    detail=f"line {line_no}: JSON 파싱 실패 — {exc}",
                )
            )
            continue
        if not isinstance(row, dict):
            issues.append(
                IntegrityIssue(
                    path=str(journal_path),
                    kind="line_corrupt",
                    detail=f"line {line_no}: dict 아님 ({type(row).__name__})",
                )
            )
            continue
        if "event" not in row or "client_id" not in row:
            issues.append(
                IntegrityIssue(
                    path=str(journal_path),
                    kind="missing_field",
                    detail=f"line {line_no}: event/client_id 누락",
                )
            )
            continue
        event = row.get("event")
        if event not in valid_events:
            issues.append(
                IntegrityIssue(
                    path=str(journal_path),
                    kind="invalid_value",
                    detail=f"line {line_no}: 알 수 없는 event 값 — {event!r}",
                    severity="warning",
                )
            )

    return issues


def run_all_checks(
    *,
    reports_root: Path,
    journal_path: Path,
) -> list[IntegrityIssue]:
    """모든 검사 실행 → 모든 issue 합쳐 반환."""
    issues: list[IntegrityIssue] = []
    issues.extend(check_daily_reports(reports_root))
    issues.extend(check_order_journal(journal_path))
    return issues


def severity_counts(issues: Iterable[IntegrityIssue]) -> tuple[int, int]:
    """(error_count, warning_count). CLI exit code 결정에 사용."""
    err = sum(1 for i in issues if i.severity == "error")
    warn = sum(1 for i in issues if i.severity == "warning")
    return err, warn
