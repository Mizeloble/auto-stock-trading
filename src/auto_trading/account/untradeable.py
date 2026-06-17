"""거래불가 종목 이력 영속 — 다음 거래일 발굴에서 사전 회피.

배경(2026-06-16): 모의투자 매매제한 종목(004870)이 발굴에 채택돼 하루 종일
매분 주문 거부(RC4007)되며 슬롯 하나를 낭비. 거부는 그날 즉시
`runner._skipped_until` 로 재시도가 막히지만, *다음 날* 발굴은 그 사실을
모른 채 같은 종목을 다시 채택할 수 있다. 종목 단위로 "오늘 거래 불가"가
확인된 종목(매매제한·거래정지·무효코드)을 디스크에 누적해, 발굴이
사전에 빼고 다음 순위로 채우게 한다.

구조 (day_state 와 동일 원칙):
- 디스크 JSON 한 파일. 외부 API 의존 0.
- 쓰기 실패는 swallow + 구조화 로그 — 디스크 문제로 트레이딩이 멈추지 않게.
- 영구 차단이 아니라 노화(`within_days`) — 한동안 안 걸리면 자동 만료해,
  모의투자 제한 해제·재상장 종목이 영원히 묻히지 않게.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

__all__ = [
    "DEFAULT_UNTRADEABLE_PATH",
    "UntradeableEntry",
    "UntradeableStore",
]

DEFAULT_UNTRADEABLE_PATH = Path("data/state/untradeable.json")

# 발굴 회피 기본 보존 기간 — 이 기간 안에 다시 안 걸리면 자동 만료.
DEFAULT_WITHIN_DAYS = 30


@dataclass
class UntradeableEntry:
    """종목 단위 거래불가 이력 한 건."""

    symbol: str
    name: str = ""
    reason: str = ""  # RejectCategory.value (예: "trading_halt")
    first_date: str = ""  # KST YYYY-MM-DD — 처음 확인
    last_date: str = ""  # KST YYYY-MM-DD — 마지막 확인
    count: int = 0  # 누적 거부 일수(같은 날 중복은 1로 합침)


@dataclass
class UntradeableStore:
    path: Path = DEFAULT_UNTRADEABLE_PATH

    def load(self) -> dict[str, UntradeableEntry]:
        """symbol → 이력. 없음/손상 → 빈 dict (트레이딩을 막지 않는다)."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("untradeable.load_failed", path=str(self.path), error=str(exc))
            return {}
        entries: dict[str, UntradeableEntry] = {}
        for sym, rec in (raw.get("symbols") or {}).items():
            if not isinstance(rec, dict):
                continue
            try:
                entries[str(sym)] = UntradeableEntry(
                    symbol=str(sym),
                    name=str(rec.get("name") or ""),
                    reason=str(rec.get("reason") or ""),
                    first_date=str(rec.get("first_date") or ""),
                    last_date=str(rec.get("last_date") or ""),
                    count=int(rec.get("count") or 0),
                )
            except TypeError, ValueError:
                continue
        return entries

    def record(
        self,
        symbol: str,
        *,
        today: date,
        name: str = "",
        reason: str = "",
    ) -> None:
        """거래불가 1건 upsert 후 저장. 같은 날 중복 호출은 count 를 안 늘린다."""
        if not symbol:
            return
        entries = self.load()
        today_iso = today.isoformat()
        existing = entries.get(symbol)
        if existing is None:
            entries[symbol] = UntradeableEntry(
                symbol=symbol,
                name=name,
                reason=reason,
                first_date=today_iso,
                last_date=today_iso,
                count=1,
            )
        else:
            # 같은 날 반복 거부(예: 62회)는 한 번만 카운트 — last_date 비교.
            if existing.last_date != today_iso:
                existing.count += 1
                existing.last_date = today_iso
            if name:
                existing.name = name
            if reason:
                existing.reason = reason
        self._save(entries)

    def active_symbols(self, today: date, *, within_days: int = DEFAULT_WITHIN_DAYS) -> set[str]:
        """`within_days` 안에 거래불가가 확인된 종목 집합 — 발굴 제외용.

        last_date 파싱 실패/공백은 보수적으로 *포함*(최근 기록일 가능성).
        """
        cutoff = today - timedelta(days=within_days)
        out: set[str] = set()
        for sym, e in self.load().items():
            try:
                last = date.fromisoformat(e.last_date)
            except ValueError:
                out.add(sym)
                continue
            if last >= cutoff:
                out.add(sym)
        return out

    def _save(self, entries: dict[str, UntradeableEntry]) -> None:
        """atomic write (tmp → rename). 실패는 swallow — 주문 경로를 막지 않는다."""
        payload = {"symbols": {sym: asdict(e) for sym, e in entries.items()}}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            log.error("untradeable.save_failed", path=str(self.path), error=str(exc))
