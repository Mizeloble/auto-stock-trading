"""시가총액 상위 N종목 마스터 산출 — KRX 공식 데이터 기반.

`monitor/price_snapshot.py` 가 매 10분 호출할 종목 풀의 *소스 오브 트루스*.

설계:
- 키움 REST API 에 시총 정렬 TR 도, 전종목 코드 리스트 TR 도 없다(`kiwoom/endpoints.py`
  참고). 따라서 KRX 공식 페이지에서 마스터를 별도 경로로 받아온다.
- 데이터 소스: `FinanceDataReader` — `fdr.StockListing('KRX')` 한 줄에 전 상장종목
  (~2,800 개) 의 Code/Name/Market/Marcap/Stocks/Close 를 DataFrame 으로 반환.
  (초기 도입은 `pykrx` 였으나 2026-05-21 시점에 KRX 의 OTP 기반 시장 다운로드
  페이지 변경으로 pykrx 시장 전체 조회 함수가 전부 KeyError 로 깨짐 — fdr 로 이전.)
- 결과는 JSON 캐시로 디스크에 저장. `snapshot-top200` 잡이 그 캐시를 읽는다.
- 일 1회 (08:50 KST) 호출 가정. 시총 데이터는 직전 거래일 종가 기준.

market/ 규칙 준수:
- 외부에는 도메인 타입(`MarketCapEntry`) 만 노출. fdr DataFrame 을 그대로 반환 X.
- 시간은 KST datetime (`_common.KST`).
- 읽기 전용 — 주문/잔고 변경 코드 없음.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

import structlog

from .._common import KST, normalize_symbol
from . import calendar as krx_calendar

log = structlog.get_logger(__name__)

Market = Literal["KOSPI", "KOSDAQ"]


@dataclass(frozen=True)
class MarketCapEntry:
    """시총 마스터 한 행. JSON 캐시 한 줄과 1:1."""

    rank: int
    symbol: str
    name: str
    market: Market
    shares: int  # 상장주식수
    close: int  # KRX 기준일 종가 (원)
    market_cap: int  # shares * close


def _resolve_query_date(base: date | None) -> date:
    """KRX 조회용 날짜 보정 — base 가 거래일이면 그대로, 아니면 직전 거래일.

    무엇/왜: pykrx 는 휴장일 호출 시 빈 DataFrame 반환 → 시총 데이터가 비어버린다.
    08:50 launchd 호출 시점에서는 오늘 종가가 아직 없으므로 *전일 종가 기준 시총*
    을 받는 게 의도된 동작이지만, 휴장 보정까지 처리해 둔다.
    """
    cursor = base or krx_calendar.today_kst()
    # 오늘이 거래일이어도 장 시작 전(08:50)에는 오늘 종가가 없으므로 직전 거래일로.
    cursor -= timedelta(days=1)
    while not krx_calendar.is_trading_day(cursor):
        cursor -= timedelta(days=1)
    return cursor


def _fetch_market(query_date: date, market: Market) -> list[tuple[str, str, int, int, int]]:
    """단일 시장 시총 데이터 — (symbol, name, market_cap, shares, close) 리스트.

    fdr 호출은 본 함수 안에만 둔다 — 모듈 import 시점에 fdr 가 실패하면
    `tests/` 가 import 단계에서 깨지지 않도록.

    fdr 의 `StockListing('KRX')` 는 query_date 인자를 받지 않고 *현재 시점* 스냅샷
    을 돌려준다 (KRX 페이지 자체가 그날의 최종 스냅샷). 따라서 `query_date` 인자는
    로깅/계약 호환용으로만 유지하고 실제 fetch 는 한 번에 끝난다.
    """
    import FinanceDataReader as fdr  # 지연 import — 매 호출마다 X.

    df = fdr.StockListing("KRX")
    if df is None or df.empty:
        log.warning("market.marketcap.empty_response", date=query_date.isoformat(), market=market)
        return []

    rows: list[tuple[str, str, int, int, int]] = []
    for _, row in df.iterrows():
        if str(row.get("Market", "")).strip() != market:
            continue
        sym = normalize_symbol(row.get("Code"))
        name = str(row.get("Name", "")).strip()
        try:
            market_cap = int(row.get("Marcap", 0) or 0)
            shares = int(row.get("Stocks", 0) or 0)
            close = int(row.get("Close", 0) or 0)
        except (TypeError, ValueError):
            continue
        if not sym or market_cap <= 0 or shares <= 0 or close <= 0:
            continue
        rows.append((sym, name, market_cap, shares, close))
    return rows


def compute_top_marketcap_universe(
    *,
    top_n: int = 200,
    base_date: date | None = None,
    markets: tuple[Market, ...] = ("KOSPI", "KOSDAQ"),
) -> list[MarketCapEntry]:
    """KOSPI+KOSDAQ 시총 desc 상위 `top_n` 마스터를 산출.

    무엇/왜:
    - `snapshot-top200` 잡이 매 10분 호출할 *종목 풀*. 하루 1회 갱신.
    - 시총 데이터는 *전일 종가 기준* — 장중 시총 변동은 무시(일 1회 갱신 의도와 일치).

    어떻게 확인:
    - 단위 테스트가 pykrx 를 monkeypatch 해 결정적 결과 검증.
    - 수동: `uv run auto-trade build-marketcap-universe --top-n 200` 후 캐시 파일 확인.
    """
    query_date = _resolve_query_date(base_date)
    merged: list[tuple[str, str, int, int, int, Market]] = []
    for market in markets:
        try:
            rows = _fetch_market(query_date, market)
        except Exception:
            log.warning(
                "market.marketcap.fetch_failed",
                market=market,
                query_date=query_date.isoformat(),
                exc_info=True,
            )
            continue
        for sym, name, market_cap, shares, close in rows:
            merged.append((sym, name, market_cap, shares, close, market))

    merged.sort(key=lambda r: r[2], reverse=True)
    chosen = merged[:top_n]
    entries = [
        MarketCapEntry(
            rank=i + 1,
            symbol=sym,
            name=name,
            market=market,
            shares=shares,
            close=close,
            market_cap=market_cap,
        )
        for i, (sym, name, market_cap, shares, close, market) in enumerate(chosen)
    ]
    log.info(
        "market.marketcap.computed",
        query_date=query_date.isoformat(),
        markets=list(markets),
        candidates=len(merged),
        top_n=top_n,
        chosen=len(entries),
        first=[(e.symbol, e.name, e.market_cap) for e in entries[:3]],
    )
    return entries


def save_universe(entries: list[MarketCapEntry], path: Path, *, top_n: int | None = None) -> None:
    """시총 마스터를 JSON 으로 저장.

    스키마는 `data/snapshots/universe-YYYY-MM-DD.json`:
    ```json
    {"generated_at": "...", "date": "...", "top_n": 200, "entries": [...]}
    ```
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(tz=KST)
    payload = {
        "generated_at": now.isoformat(),
        "date": now.date().isoformat(),
        "top_n": top_n if top_n is not None else len(entries),
        "entries": [asdict(e) for e in entries],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("market.marketcap.saved", path=str(path), count=len(entries))


def load_cached_universe(path: Path) -> list[MarketCapEntry]:
    """저장된 시총 마스터 JSON 을 다시 `MarketCapEntry` 리스트로 로드.

    잘못된 파일/필드 누락은 빈 리스트로 폴백(호출자가 fallback_to_yesterday 결정).
    """
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("market.marketcap.cache_corrupt", path=str(path), error=str(exc))
        return []
    raw_entries = data.get("entries", [])
    out: list[MarketCapEntry] = []
    for r in raw_entries:
        try:
            out.append(
                MarketCapEntry(
                    rank=int(r["rank"]),
                    symbol=str(r["symbol"]),
                    name=str(r.get("name", "")),
                    market=r["market"],
                    shares=int(r["shares"]),
                    close=int(r["close"]),
                    market_cap=int(r["market_cap"]),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("market.marketcap.cache_row_skipped", path=str(path), error=str(exc))
    return out


def universe_path_for(out_dir: Path, when: date | None = None) -> Path:
    """`out_dir/universe-YYYY-MM-DD.json` — 캐시 파일 표준 경로."""
    d = when or krx_calendar.today_kst()
    return out_dir / f"universe-{d.isoformat()}.json"


__all__ = [
    "Market",
    "MarketCapEntry",
    "compute_top_marketcap_universe",
    "load_cached_universe",
    "save_universe",
    "universe_path_for",
]
