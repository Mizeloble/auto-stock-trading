"""시총 상위 종목 일중 스냅샷(`data/snapshots/topcap-YYYY-MM-DD.jsonl`) read-only 리더.

`monitor/price_snapshot.py` 가 매 10분 append 하는 JSONL 을 사후에 읽어, 일일 리포트가
체결 시각과 조인할 수 있는 종목별 시계열로 만든다. 데이터 기반 전략 분석용 — 진입
시점 등락률·순위, 보유구간 MFE/MAE, 차단 종목의 그날 움직임 복원에 쓰인다.

설계 — `log_parser.py` 와 같은 정신:
- JSON 라인만 파싱, 손상/누락 라인은 조용히 skip. 비-JSON 헤더도 무시.
- 외부 호출 없음(reports/ 규칙). 입력은 로컬 파일(topcap + 있으면 discovered),
  출력은 결정론적.
- 파일이 없으면 빈 리더 — 모든 조회가 None 으로 우아하게 폴백(과거 일자·게이트 차단일).

시각 매칭: 스냅샷 ts 는 KST 오프셋 포함 ISO 8601("...+09:00"). 체결 시각은 "HH:MM"
(daily.py 가 로그 timestamp 를 변환한 것)이라, 양쪽을 자정 기준 분(分)으로 환산해
가장 가까운 포인트를 고른다(스냅샷 캐던스 10분이라 근사 — 한계는 호출부 주석).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class SnapshotPoint:
    """한 종목의 한 시점 스냅샷(분석에 필요한 필드만)."""

    minute: int  # 자정 기준 분(KST). 0..1439
    price: float
    change_pct: float  # 전일 대비 등락률 %
    rank: int  # 그날 시총 순위


@dataclass
class TopcapSnapshots:
    """종목 → 시각순 SnapshotPoint 리스트. 조회 헬퍼 제공."""

    series: dict[str, list[SnapshotPoint]] = field(default_factory=dict)

    # ---- 조회 ----------------------------------------------------------
    def _nearest(self, symbol: str, minute: int) -> SnapshotPoint | None:
        pts = self.series.get(symbol)
        if not pts:
            return None
        return min(pts, key=lambda p: abs(p.minute - minute))

    def change_pct_at(self, symbol: str, hhmm: str | None) -> float | None:
        """hhmm 시각에 가장 가까운 스냅샷의 등락률(%). 없으면 None."""
        m = _hhmm_to_minute(hhmm)
        if m is None:
            return None
        pt = self._nearest(symbol, m)
        return pt.change_pct if pt is not None else None

    def rank_at(self, symbol: str, hhmm: str | None) -> int | None:
        """hhmm 시각에 가장 가까운 스냅샷의 시총 순위. 없으면 None."""
        m = _hhmm_to_minute(hhmm)
        if m is None:
            return None
        pt = self._nearest(symbol, m)
        return pt.rank if pt is not None else None

    def range_during(
        self, symbol: str, start_hhmm: str | None, end_hhmm: str | None
    ) -> tuple[float, float] | None:
        """[start, end] 보유구간의 (최저가, 최고가). 구간 내 포인트가 없으면 None.

        MFE/MAE 추정용. 경계 시각이 결손이면 None(부분 추정은 오해 소지).
        """
        s = _hhmm_to_minute(start_hhmm)
        e = _hhmm_to_minute(end_hhmm)
        pts = self.series.get(symbol)
        if s is None or e is None or not pts:
            return None
        if e < s:
            s, e = e, s
        prices = [p.price for p in pts if s <= p.minute <= e]
        if not prices:
            return None
        return min(prices), max(prices)

    def day_outcome(self, symbol: str) -> tuple[float | None, float | None]:
        """(장중 시초→종가 변화율 %, 종가 기준 전일대비 등락률 %).

        차단 종목 기회비용용. 첫 포인트→마지막 포인트 가격으로 일중 수익률을,
        마지막 포인트 change_pct 로 전일 대비 일간 등락률을 돌려준다. 포인트가
        없거나 1개뿐이면 일중 수익률은 None.
        """
        pts = self.series.get(symbol)
        if not pts:
            return None, None
        first, last = pts[0], pts[-1]
        intraday = None
        if first.price != 0 and len(pts) >= 2:
            intraday = (last.price / first.price - 1.0) * 100.0
        return intraday, last.change_pct


def parse_topcap_snapshots(path: Path | str) -> TopcapSnapshots:
    """topcap-<date>.jsonl → TopcapSnapshots. 파일 없거나 읽기 실패면 빈 리더."""
    snap = TopcapSnapshots()
    _parse_into(snap, Path(path))
    _sort_series(snap)
    return snap


def parse_topcap_for_date(date: str, *, root: Path | str = "data/snapshots") -> TopcapSnapshots:
    """`<root>/topcap-<date>.jsonl` + `<root>/discovered-<date>.jsonl` 병합 로드.

    discovered 파일(2026-07-04 커버리지 확장)은 그날 발굴된 시총 200 밖 종목의
    10분 스냅샷 — 이걸 합쳐야 거래량 발굴 소형주도 진입 등락률·MFE/MAE 조인이
    가능하다. 파일이 없으면(과거 일자·휴장일) topcap 만으로 동작 — 회귀 0.
    breadth 게이트는 이 리더가 아니라 topcap 원본 파일을 직접 읽으므로 무영향.
    """
    snap = TopcapSnapshots()
    _parse_into(snap, Path(root) / f"topcap-{date}.jsonl")
    _parse_into(snap, Path(root) / f"discovered-{date}.jsonl")
    _sort_series(snap)
    return snap


def _parse_into(snap: TopcapSnapshots, path: Path) -> None:
    """JSONL 한 파일을 기존 리더에 누적 파싱. 없음/읽기 실패는 조용히 no-op."""
    if not path.exists():
        return
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                _ingest(snap, line)
    except OSError:
        return


def _sort_series(snap: TopcapSnapshots) -> None:
    # 시각순 정렬 — append 순서가 곧 시각순이지만 병합·방어 목적 정렬.
    for pts in snap.series.values():
        pts.sort(key=lambda pt: pt.minute)


def _ingest(snap: TopcapSnapshots, line: str) -> None:
    line = line.strip()
    if not line or line[0] != "{":
        return
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        return
    symbol = rec.get("symbol")
    minute = _iso_to_minute(rec.get("ts"))
    price = _as_float(rec.get("price"))
    if not isinstance(symbol, str) or not symbol or minute is None or price is None:
        return
    change_pct = _as_float(rec.get("change_pct")) or 0.0
    rank_raw = rec.get("rank")
    rank = rank_raw if isinstance(rank_raw, int) else 0
    snap.series.setdefault(symbol, []).append(
        SnapshotPoint(minute=minute, price=price, change_pct=change_pct, rank=rank)
    )


def _iso_to_minute(raw: object) -> int | None:
    """ISO 8601(KST 오프셋 포함) → 자정 기준 분. 파싱 실패면 None."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt.hour * 60 + dt.minute


def _hhmm_to_minute(hhmm: str | None) -> int | None:
    if not hhmm:
        return None
    try:
        h, m = (int(x) for x in hhmm.split(":"))
    except ValueError, AttributeError:
        return None
    return h * 60 + m


def _as_float(raw: object) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return None


__all__ = [
    "SnapshotPoint",
    "TopcapSnapshots",
    "parse_topcap_for_date",
    "parse_topcap_snapshots",
]
