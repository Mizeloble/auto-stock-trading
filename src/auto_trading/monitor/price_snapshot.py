"""시총 상위 N종목 10분 간격 현재가 스냅샷 기록.

매 10분 launchd 잡이 호출 → `market/marketcap.py` 가 만든 universe 캐시에서
종목 리스트 로드 → 각 종목 `ka10001` (`market/quote.py::get_quote`) → JSONL append.

설계 — `monitor/latency.py::LatencyTracker` 와 동일 정신:
- JSONL append-only, 손상 라인은 replay 시 스킵, 디스크 오류는 swallow + 로그.
- best-effort: 한 종목 실패가 라운드 전체를 깨지 않음.
- monitor/ 규칙: read-only, 외부 호출 없음(현재가 호출은 인자로 받은 client 사용).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Protocol

import structlog

from .._common import KST
from ..kiwoom.rest import KiwoomRestClient
from ..market.index import Market as IndexMarket
from ..market.index import fetch_latest_index_price
from ..market.quote import get_quote

# NOTE: MarketCapEntry 는 SnapshotItem 프로토콜을 구조적으로 만족한다 — 직접 import 불필요.

log = structlog.get_logger(__name__)


class SnapshotItem(Protocol):
    """스냅샷 대상의 최소 계약 — `MarketCapEntry` 와 `SnapshotTarget` 이 모두 만족.

    read-only property 로 선언해 frozen dataclass(Literal market 포함)가 구조적으로
    매칭되게 한다.
    """

    @property
    def rank(self) -> int: ...
    @property
    def symbol(self) -> str: ...
    @property
    def name(self) -> str: ...
    @property
    def market(self) -> str: ...


@dataclass(frozen=True)
class SnapshotTarget:
    """시총 마스터 밖의 스냅샷 대상(발굴 종목 등). rank=0 = 시총 순위 아님."""

    symbol: str
    name: str = ""
    market: str = ""
    rank: int = 0


@dataclass(frozen=True)
class PriceSnapshotEntry:
    """JSONL 한 줄 = 한 종목의 한 시점 스냅샷."""

    ts: str  # ISO 8601 KST
    rank: int  # 그 날 시총 마스터에서의 순위
    symbol: str
    name: str
    market: str  # "KOSPI" / "KOSDAQ"
    price: float  # 현재가 (절댓값)
    change: float  # 전일 대비 변동폭 (부호 포함)
    change_pct: float  # 등락률 %
    volume: int  # 누적 거래량


def snapshot_path_for(out_dir: Path, when: date | None = None) -> Path:
    """`out_dir/topcap-YYYY-MM-DD.jsonl` — 일별 회전 파일 표준 경로."""
    d = when or datetime.now(tz=KST).date()
    return out_dir / f"topcap-{d.isoformat()}.jsonl"


def discovered_snapshot_path_for(out_dir: Path, when: date | None = None) -> Path:
    """`out_dir/discovered-YYYY-MM-DD.jsonl` — 발굴 종목 스냅샷 일별 파일.

    topcap 과 **파일을 분리**하는 이유: breadth 게이트(`market/breadth.py`)가
    topcap 파일의 '마지막 라운드 줄 수'로 시장 폭을 판단하므로, 발굴 종목을 같은
    파일에 섞으면 표본이 오염된다. 리포트 조인(`reports/snapshot_reader`)은
    두 파일을 합쳐 읽는다.
    """
    d = when or datetime.now(tz=KST).date()
    return out_dir / f"discovered-{d.isoformat()}.jsonl"


def load_todays_discovery_symbols(
    alt_rank_path: Path,
    *,
    exclude_symbols: set[str] | None = None,
    when: date | None = None,
) -> list[SnapshotTarget]:
    """`data/discovery/alt-rank.jsonl` 에서 오늘 발굴된 종목들을 스냅샷 대상으로 추출.

    무엇/왜: topcap 스냅샷은 시총 상위만 담아 거래량 발굴 소형주의 일중 데이터가
    비어 있었다(진입 피처·MFE/MAE None — memory: project_snapshot_join_coverage).
    발굴 기록(volume_pool + value_pool, PR #164)을 읽어 그 종목들도 10분 스냅샷
    대상에 추가한다. read-only 로컬 파일 파싱 — 외부 호출 없음(monitor/ 규칙).

    - 오늘(when) 날짜 레코드만 사용. 손상 라인·파일 없음은 빈 리스트.
    - `exclude_symbols`(보통 topcap universe)에 이미 있는 종목은 제외 — 중복 호출 방지.
    - 이름·시장은 value_pool 레코드에서 보강(volume_pool 은 코드만 기록됨).
    """
    target_day = (when or datetime.now(tz=KST).date()).isoformat()
    excluded = exclude_symbols or set()
    if not alt_rank_path.exists():
        return []
    symbols: dict[str, SnapshotTarget] = {}
    try:
        lines = alt_rank_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        log.warning("snapshot.alt_rank_read_failed", path=str(alt_rank_path), error=str(exc))
        return []
    for raw in lines:
        raw = raw.strip()
        if not raw or raw[0] != "{":
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if rec.get("date") != target_day:
            continue
        for sym in rec.get("volume_pool") or []:
            if isinstance(sym, str) and sym and sym not in excluded:
                symbols.setdefault(sym, SnapshotTarget(symbol=sym))
        for row in rec.get("value_pool") or []:
            if not isinstance(row, dict):
                continue
            sym = row.get("symbol")
            if not isinstance(sym, str) or not sym or sym in excluded:
                continue
            # value_pool 엔 이름·시장이 있으므로 volume_pool 만의 항목도 보강.
            symbols[sym] = SnapshotTarget(
                symbol=sym,
                name=str(row.get("name") or ""),
                market=str(row.get("market") or ""),
            )
    return list(symbols.values())


@dataclass
class PriceSnapshotRecorder:
    """JSONL append + best-effort.

    경로는 운영에서 `data/snapshots/topcap-YYYY-MM-DD.jsonl`.
    """

    path: Path

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _append(self, entry: PriceSnapshotEntry) -> None:
        line = json.dumps(asdict(entry), ensure_ascii=False) + "\n"
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError as exc:
            log.error("snapshot.write_failed", path=str(self.path), error=str(exc))

    async def record_round(
        self,
        client: KiwoomRestClient,
        universe: list[SnapshotItem],
        *,
        now: datetime | None = None,
    ) -> list[PriceSnapshotEntry]:
        """universe 의 각 종목 현재가를 한 번씩 조회해 JSONL 한 라운드 append.

        - 한 종목 호출 실패는 swallow + 로그 — 다른 종목 계속.
        - 라운드 안에서 ts 는 라운드 시작 시각으로 고정(같은 timestamp 가 200줄).
        - 키움 페이싱(150ms)으로 200종목 ≈ 30초 소요.
        - 입력은 `SnapshotItem` 계약(rank/symbol/name/market)만 요구 —
          시총 마스터(`MarketCapEntry`)와 발굴 대상(`SnapshotTarget`) 공용.
        """
        ts_iso = (now or datetime.now(tz=KST)).isoformat()
        out: list[PriceSnapshotEntry] = []
        ok_count = 0
        fail_count = 0
        for item in universe:
            try:
                quote = await get_quote(client, item.symbol)
            except Exception as exc:
                fail_count += 1
                log.warning(
                    "snapshot.quote_failed",
                    symbol=item.symbol,
                    name=item.name,
                    error=str(exc),
                )
                continue
            entry = PriceSnapshotEntry(
                ts=ts_iso,
                rank=item.rank,
                symbol=item.symbol,
                name=item.name,
                market=item.market,
                price=float(quote.price),
                change=float(quote.change),
                change_pct=float(quote.change_pct),
                volume=int(quote.volume),
            )
            self._append(entry)
            out.append(entry)
            ok_count += 1
        log.info(
            "snapshot.round_done",
            path=str(self.path),
            universe_size=len(universe),
            ok=ok_count,
            fail=fail_count,
            ts=ts_iso,
        )
        return out


@dataclass(frozen=True)
class IndexSnapshotEntry:
    """JSONL 한 줄 = 한 지수의 한 시점 스냅샷.

    `market/index.py::fetch_latest_index_price` 가 돌려주는 "ka20009 의 가장 최근
    일봉 close" — 장중 호출 시 그 날 행이 실시간 갱신되므로 사실상 현재 지수값.
    """

    ts: str  # ISO 8601 KST
    market: str  # "KOSPI" / "KOSDAQ"
    value: float  # 지수 포인트 (예: 7271.66)


def index_snapshot_path_for(out_dir: Path, when: date | None = None) -> Path:
    """`out_dir/indices-YYYY-MM-DD.jsonl` — 지수 일별 회전 파일."""
    d = when or datetime.now(tz=KST).date()
    return out_dir / f"indices-{d.isoformat()}.jsonl"


@dataclass
class IndexSnapshotRecorder:
    """KOSPI/KOSDAQ 종합지수 현재가 JSONL append.

    설계는 `PriceSnapshotRecorder` 와 동일 — best-effort, 디스크 오류 swallow.
    1라운드 = 2지수 = 2회 ka20009 호출 ≈ 0.3초.
    """

    path: Path

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _append(self, entry: IndexSnapshotEntry) -> None:
        line = json.dumps(asdict(entry), ensure_ascii=False) + "\n"
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError as exc:
            log.error("index_snapshot.write_failed", path=str(self.path), error=str(exc))

    async def record_round(
        self,
        client: KiwoomRestClient,
        markets: tuple[IndexMarket, ...] = ("KOSPI", "KOSDAQ"),
        *,
        now: datetime | None = None,
    ) -> list[IndexSnapshotEntry]:
        """각 지수 1회씩 ka20009 호출 → JSONL append.

        - `fetch_latest_index_price` 가 None 반환 시 그 지수는 스킵.
        - 한 지수 실패가 다른 지수 기록을 막지 않음.
        """
        ts_iso = (now or datetime.now(tz=KST)).isoformat()
        out: list[IndexSnapshotEntry] = []
        for market in markets:
            try:
                value = await fetch_latest_index_price(client, market)
            except Exception as exc:
                log.warning("index_snapshot.fetch_failed", market=market, error=str(exc))
                continue
            if value is None:
                log.warning("index_snapshot.no_data", market=market)
                continue
            entry = IndexSnapshotEntry(ts=ts_iso, market=market, value=float(value))
            self._append(entry)
            out.append(entry)
        log.info(
            "index_snapshot.round_done",
            path=str(self.path),
            ok=len(out),
            ts=ts_iso,
        )
        return out


__all__ = [
    "IndexSnapshotEntry",
    "IndexSnapshotRecorder",
    "PriceSnapshotEntry",
    "PriceSnapshotRecorder",
    "SnapshotItem",
    "SnapshotTarget",
    "discovered_snapshot_path_for",
    "index_snapshot_path_for",
    "load_todays_discovery_symbols",
    "snapshot_path_for",
]
