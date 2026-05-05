"""DailyReport 도메인 + 로그·스냅샷 → 리포트 빌더 + JSON I/O.

빌더는 두 입력만 사용:
1. `LogEvents` (그날 로그 파일 파싱 결과)
2. `RunnerStateSnapshot` (러너의 인메모리 상태) — 선택적, 종료 직후만 가용

LLM 호출·외부 API 호출 없음. 결정론적 — 같은 입력이면 같은 출력.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..notify.state import RunnerStateSnapshot
from .log_parser import LogEvents


@dataclass(frozen=True)
class TradeRecord:
    """매수·매도 1건. price 는 로그에 남아있을 때만(시장가는 None)."""

    symbol: str
    side: str  # "buy" / "sell"
    quantity: int
    price: float | None
    simulated: bool  # dry-run 시뮬이면 True


@dataclass(frozen=True)
class LiquidationRecord:
    """청산 1건. 매수 가격까지 알면 buy_price 채움 → pnl 계산."""

    symbol: str
    quantity: int
    sell_price: float | None
    buy_price: float | None
    simulated: bool

    @property
    def pnl(self) -> float | None:
        if self.sell_price is None or self.buy_price is None:
            return None
        return (self.sell_price - self.buy_price) * self.quantity

    @property
    def pnl_pct(self) -> float | None:
        if self.sell_price is None or self.buy_price is None or self.buy_price == 0:
            return None
        return (self.sell_price / self.buy_price - 1.0) * 100.0


@dataclass(frozen=True)
class MarketGateInfo:
    """시장 게이트 1건 (KOSPI / KOSDAQ)."""

    market: str
    last: float | None
    sma5: float | None
    sma20: float | None
    above: bool


@dataclass(frozen=True)
class DailyReport:
    """하루치 트레이딩 요약. JSON 직렬화 → `data/reports/daily-YYYY-MM-DD.json`."""

    date: str  # YYYY-MM-DD
    mode: str  # "day-trade" / "swing"
    dry_run: bool
    discovery_chosen: list[str]
    discovery_blocked: list[tuple[str, str]]
    market_gates: list[MarketGateInfo]
    buys: list[TradeRecord]
    liquidations: list[LiquidationRecord]
    error_count: int
    note_ko: str
    # 메타. 누적 손익(가능한 경우만 채워진다 — 시장가 매수/매도 가격이 둘 다 있을 때).
    realized_pnl: float | None = None
    realized_pnl_pct: float | None = None
    excluded_etf_count: int = 0
    rank_candidates: list[str] = field(default_factory=list)


# ---- 빌더 ---------------------------------------------------------------


def build_daily_report(
    *,
    date: str,
    events: LogEvents,
    snapshot: RunnerStateSnapshot | None,
) -> DailyReport:
    """LogEvents + 옵셔널 snapshot 으로 DailyReport 생성.

    무엇/왜:
    - 자동 호출(러너 종료 직후) 은 snapshot 이 있어 mode/dry_run 을 알 수 있음.
    - 수동 재생성(`auto-trade report --date …`) 은 snapshot=None — 로그만 보고 추정.
    - day-trade 가 아닌 일자(레거시 swing 또는 데이터 없음) 도 빈 DailyReport 로 안전하게 만듦.
    """
    discovery_chosen, discovery_blocked = _extract_discovery(events)
    market_gates = _extract_gates(events)
    buys, liquidations = _extract_trades(events)
    error_count = (
        len(events.orders_failed)
        + len(events.liquidations_failed)
        + len(events.fatal)
        + (1 if events.balance_failures > 0 else 0)
    )

    # snapshot 우선. 없으면 events 로 mode/dry_run 추정.
    if snapshot is not None:
        mode = snapshot.runner_mode
        dry_run = snapshot.dry_run
    else:
        mode = "day-trade" if (events.discovery is not None or events.rank is not None) else "swing"
        dry_run = bool(events.orders_simulated or events.liquidations_simulated)

    realized_pnl, realized_pnl_pct = _aggregate_pnl(liquidations)

    note_ko = _summary_ko(
        chosen=discovery_chosen,
        blocked=discovery_blocked,
        liquidations=liquidations,
        realized_pnl=realized_pnl,
        error_count=error_count,
    )

    rank_candidates: list[str] = []
    excluded = 0
    if events.rank is not None:
        chosen = events.rank.get("chosen") or []
        # chosen 항목은 [symbol, market, volume] 또는 단순 symbol 리스트일 수 있음 — 양쪽 처리.
        for item in chosen:
            if isinstance(item, list | tuple) and item:
                rank_candidates.append(str(item[0]))
            elif isinstance(item, str):
                rank_candidates.append(item)
        excluded_raw = events.rank.get("excluded_count")
        if isinstance(excluded_raw, int):
            excluded = excluded_raw

    return DailyReport(
        date=date,
        mode=mode,
        dry_run=dry_run,
        discovery_chosen=discovery_chosen,
        discovery_blocked=discovery_blocked,
        market_gates=market_gates,
        buys=buys,
        liquidations=liquidations,
        error_count=error_count,
        note_ko=note_ko,
        realized_pnl=realized_pnl,
        realized_pnl_pct=realized_pnl_pct,
        excluded_etf_count=excluded,
        rank_candidates=rank_candidates,
    )


def _extract_discovery(events: LogEvents) -> tuple[list[str], list[tuple[str, str]]]:
    if events.discovery is None:
        return [], []
    chosen_raw = events.discovery.get("chosen") or []
    chosen: list[str] = [str(s) for s in chosen_raw if isinstance(s, str)]
    blocked_raw = events.discovery.get("blocked") or []
    blocked: list[tuple[str, str]] = []
    for item in blocked_raw:
        if isinstance(item, list | tuple) and len(item) >= 2:
            blocked.append((str(item[0]), str(item[1])))
    return chosen, blocked


def _extract_gates(events: LogEvents) -> list[MarketGateInfo]:
    out: list[MarketGateInfo] = []
    seen: set[str] = set()
    for rec in events.market_gates:
        market = str(rec.get("market", ""))
        if not market or market in seen:
            continue
        seen.add(market)
        out.append(
            MarketGateInfo(
                market=market,
                last=_as_float(rec.get("last")),
                sma5=_as_float(rec.get("sma5")),
                sma20=_as_float(rec.get("sma20")),
                above=bool(rec.get("above")),
            )
        )
    return out


def _extract_trades(events: LogEvents) -> tuple[list[TradeRecord], list[LiquidationRecord]]:
    buys: list[TradeRecord] = []
    buy_prices: dict[str, float] = {}  # symbol -> last buy price (시뮬·실거래 공통)
    for rec in events.orders_simulated:
        side = str(rec.get("side", ""))
        if side != "buy":
            continue
        symbol = str(rec.get("symbol", ""))
        qty = _as_int(rec.get("qty")) or 0
        price = _as_float(rec.get("price"))
        if symbol and qty > 0:
            buys.append(
                TradeRecord(symbol=symbol, side="buy", quantity=qty, price=price, simulated=True)
            )
            if price is not None:
                buy_prices[symbol] = price
    for rec in events.orders_accepted:
        side = str(rec.get("side", ""))
        if side != "buy":
            continue
        symbol = str(rec.get("symbol", ""))
        qty = _as_int(rec.get("qty")) or 0
        if symbol and qty > 0:
            buys.append(
                TradeRecord(symbol=symbol, side="buy", quantity=qty, price=None, simulated=False)
            )

    liqs: list[LiquidationRecord] = []
    for rec in events.liquidations_simulated:
        symbol = str(rec.get("symbol", ""))
        qty = _as_int(rec.get("qty")) or 0
        if symbol and qty > 0:
            liqs.append(
                LiquidationRecord(
                    symbol=symbol,
                    quantity=qty,
                    sell_price=None,  # 시뮬 청산은 시장가 — 가격 미기록
                    buy_price=buy_prices.get(symbol),
                    simulated=True,
                )
            )
    for rec in events.liquidations_accepted:
        symbol = str(rec.get("symbol", ""))
        qty = _as_int(rec.get("qty")) or 0
        if symbol and qty > 0:
            liqs.append(
                LiquidationRecord(
                    symbol=symbol,
                    quantity=qty,
                    sell_price=None,
                    buy_price=buy_prices.get(symbol),
                    simulated=False,
                )
            )
    return buys, liqs


def _aggregate_pnl(liqs: list[LiquidationRecord]) -> tuple[float | None, float | None]:
    """가격 정보가 있는 청산만 집계. 하나라도 가격 없으면 None."""
    if not liqs:
        return None, None
    total_pnl = 0.0
    total_cost = 0.0
    for li in liqs:
        if li.sell_price is None or li.buy_price is None:
            return None, None
        total_pnl += li.pnl or 0.0
        total_cost += li.buy_price * li.quantity
    if total_cost == 0:
        return None, None
    return total_pnl, total_pnl / total_cost * 100.0


def _summary_ko(
    *,
    chosen: list[str],
    blocked: list[tuple[str, str]],
    liquidations: list[LiquidationRecord],
    realized_pnl: float | None,
    error_count: int,
) -> str:
    if not chosen and not liquidations:
        if blocked:
            return f"매수 후보 {len(blocked)}건 모두 게이트 차단 — 거래 없음."
        return "거래 없음."
    parts: list[str] = []
    if chosen:
        parts.append(
            f"매수 {len(chosen)}건 ({', '.join(chosen[:3])}{'…' if len(chosen) > 3 else ''})"
        )
    if liquidations:
        parts.append(f"청산 {len(liquidations)}건")
    if realized_pnl is not None:
        sign = "+" if realized_pnl >= 0 else ""
        parts.append(f"실현 {sign}{int(realized_pnl):,}원")
    if error_count > 0:
        parts.append(f"에러 {error_count}건")
    return " · ".join(parts)


def _as_float(raw: object) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return None


def _as_int(raw: object) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)  # type: ignore[call-overload]
    except TypeError, ValueError:
        return None


# ---- I/O -----------------------------------------------------------------


def save_daily_report(report: DailyReport, *, root: Path | str = "data/reports") -> Path:
    """`<root>/daily-<date>.json` 으로 저장. 디렉터리 자동 생성."""
    out_dir = Path(root)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"daily-{report.date}.json"
    payload = _to_json_dict(report)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


def load_daily_report(path: Path | str) -> DailyReport:
    """저장된 JSON → DailyReport 라운드트립."""
    raw = Path(path).read_text(encoding="utf-8")
    payload = json.loads(raw)
    return _from_json_dict(payload)


def _to_json_dict(report: DailyReport) -> dict[str, Any]:
    d = asdict(report)
    # tuple → list (JSON 호환)
    d["discovery_blocked"] = [list(t) for t in report.discovery_blocked]
    return d


def _from_json_dict(payload: dict[str, Any]) -> DailyReport:
    return DailyReport(
        date=payload["date"],
        mode=payload["mode"],
        dry_run=payload["dry_run"],
        discovery_chosen=list(payload.get("discovery_chosen") or []),
        discovery_blocked=[
            (str(t[0]), str(t[1]))
            for t in (payload.get("discovery_blocked") or [])
            if isinstance(t, list | tuple) and len(t) >= 2
        ],
        market_gates=[MarketGateInfo(**g) for g in payload.get("market_gates") or []],
        buys=[TradeRecord(**t) for t in payload.get("buys") or []],
        liquidations=[LiquidationRecord(**li) for li in payload.get("liquidations") or []],
        error_count=int(payload.get("error_count") or 0),
        note_ko=str(payload.get("note_ko") or ""),
        realized_pnl=payload.get("realized_pnl"),
        realized_pnl_pct=payload.get("realized_pnl_pct"),
        excluded_etf_count=int(payload.get("excluded_etf_count") or 0),
        rank_candidates=list(payload.get("rank_candidates") or []),
    )


__all__ = [
    "DailyReport",
    "LiquidationRecord",
    "MarketGateInfo",
    "TradeRecord",
    "build_daily_report",
    "load_daily_report",
    "save_daily_report",
]
