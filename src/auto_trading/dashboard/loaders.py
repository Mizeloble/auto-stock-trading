"""대시보드용 데이터 로더 — 모두 read-only, 외부 호출 0.

`data/reports/daily-*.json` + `data/backtests/<run>/{report.json,equity.csv,trades.csv}`
만 읽는다. 결정론적: 같은 입력이면 같은 출력.
"""

from __future__ import annotations

import csv
import json
import re
from collections import defaultdict, deque
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from ..reports.daily import load_daily_report

logger = structlog.get_logger(__name__)

_DAILY_RE = re.compile(r"^daily-(\d{4}-\d{2}-\d{2})\.json$")
_RUN_NAME_RE = re.compile(r"^[A-Za-z0-9._\-]+$")  # path traversal 차단


def list_daily_dates(reports_root: Path) -> list[str]:
    """`daily-YYYY-MM-DD.json` 파일명에서 날짜만 추출, 신순 정렬."""
    if not reports_root.exists() or not reports_root.is_dir():
        return []
    dates: list[str] = []
    for entry in reports_root.iterdir():
        if not entry.is_file():
            continue
        m = _DAILY_RE.match(entry.name)
        if m:
            dates.append(m.group(1))
    dates.sort(reverse=True)
    return dates


def load_daily(reports_root: Path, date: str) -> dict[str, Any] | None:
    """daily-<date>.json → dict. 파일 없거나 깨졌으면 None."""
    if not _is_iso_date(date):
        return None
    path = reports_root / f"daily-{date}.json"
    if not path.exists():
        return None
    try:
        report = load_daily_report(path)
    except (json.JSONDecodeError, KeyError, OSError, ValueError) as exc:
        logger.warning("dashboard.daily_parse_failed", date=date, error=str(exc))
        return None
    payload = asdict(report)
    payload["discovery_blocked"] = [list(t) for t in report.discovery_blocked]
    return payload


def equity_series(reports_root: Path) -> list[dict[str, Any]]:
    """모든 daily 리포트에서 자산 시계열 합성 (date asc).

    - balance: `closing_equity` (러너 종료 직후 키움 잔고). 자동 호출이 아닌 수동 재생성된
      리포트는 None 일 수 있다.
    - daily_pnl: `realized_pnl_after_cost` 우선, 없으면 `realized_pnl`.
    - cumulative_pnl: daily_pnl 누적합 (None 인 날은 가산하지 않음).
    """
    dates = sorted(list_daily_dates(reports_root))
    points: list[dict[str, Any]] = []
    cumulative = 0.0
    for d in dates:
        payload = load_daily(reports_root, d)
        if payload is None:
            continue
        raw_pnl = payload.get("realized_pnl_after_cost")
        if raw_pnl is None:
            raw_pnl = payload.get("realized_pnl")
        daily_pnl = float(raw_pnl) if raw_pnl is not None else None
        if daily_pnl is not None:
            cumulative += daily_pnl
        points.append(
            {
                "date": d,
                "balance": payload.get("closing_equity"),
                "daily_pnl": daily_pnl,
                "cumulative_pnl": cumulative,
            }
        )
    return points


def list_backtests(backtests_root: Path) -> list[dict[str, Any]]:
    """백테스트 결과 폴더 스캔. `report.json` 이 있는 폴더만 노출."""
    if not backtests_root.exists() or not backtests_root.is_dir():
        return []
    runs: list[dict[str, Any]] = []
    for entry in backtests_root.iterdir():
        if not entry.is_dir():
            continue
        if not (entry / "report.json").exists():
            continue
        if not _RUN_NAME_RE.match(entry.name):
            continue
        runs.append({"name": entry.name, "created_at": _stat_iso(entry)})
    runs.sort(key=lambda r: r["created_at"], reverse=True)
    return runs


def load_backtest(backtests_root: Path, run: str) -> dict[str, Any] | None:
    """백테스트 폴더 3파일 → 통합 dict. 폴더 없거나 이름이 부적합하면 None."""
    if not _RUN_NAME_RE.match(run):
        return None
    run_dir = backtests_root / run
    try:
        run_dir.relative_to(backtests_root.resolve())
    except ValueError:
        return None
    if not run_dir.is_dir():
        return None
    report_path = run_dir / "report.json"
    if not report_path.exists():
        return None
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("dashboard.backtest_parse_failed", run=run, error=str(exc))
        return None

    equity = _load_equity_csv(run_dir / "equity.csv")
    trades = _load_trades_csv(run_dir / "trades.csv")
    symbol_pnl = _aggregate_symbol_pnl(trades)
    return {
        "name": run,
        "report": report,
        "equity": equity,
        "trades": trades,
        "symbol_pnl": symbol_pnl,
    }


def _is_iso_date(s: str) -> bool:
    if not isinstance(s, str) or len(s) != 10:
        return False
    try:
        datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _stat_iso(p: Path) -> str:
    return datetime.fromtimestamp(p.stat().st_mtime, tz=UTC).isoformat()


def _load_equity_csv(path: Path) -> list[dict[str, Any]]:
    """equity.csv → [{date, equity}]. pandas to_csv 형식 (첫 컬럼=인덱스, 둘째=값)."""
    if not path.exists():
        return []
    points: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.reader(fh)
            header = next(reader, None)
            if header is None:
                return []
            for row in reader:
                if len(row) < 2:
                    continue
                date_raw = row[0].strip()
                try:
                    equity = float(row[1])
                except ValueError:
                    continue
                # ISO datetime 이면 날짜 부분만, 이미 date 면 그대로.
                date_str = date_raw.split("T")[0].split(" ")[0]
                points.append({"date": date_str, "equity": equity})
    except OSError:
        return []
    return points


def _load_trades_csv(path: Path) -> list[dict[str, Any]]:
    """trades.csv → [{ts, symbol, side, price, quantity, fee, tax}]."""
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for r in reader:
                try:
                    rows.append(
                        {
                            "ts": r["ts"],
                            "symbol": r["symbol"],
                            "side": r["side"],
                            "price": float(r["price"]),
                            "quantity": int(r["quantity"]),
                            "fee": float(r.get("fee") or 0),
                            "tax": float(r.get("tax") or 0),
                        }
                    )
                except KeyError, TypeError, ValueError:
                    continue
    except OSError:
        return []
    return rows


def _aggregate_symbol_pnl(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """FIFO 매칭으로 종목별 라운드트립 손익 합산.

    metrics._round_trips 와 같은 정신이지만 dataclass(Side enum) 의존을 피해 단순 dict 처리.
    매수+매도 fee/tax 를 비례 분배해 차감.
    """
    opens: dict[str, deque[list[float]]] = defaultdict(deque)
    pnls: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    for t in trades:
        sym = str(t.get("symbol", ""))
        if not sym:
            continue
        side = str(t.get("side", "")).lower()
        qty = int(t.get("quantity", 0))
        if qty <= 0:
            continue
        price = float(t.get("price", 0.0))
        fee = float(t.get("fee", 0.0))
        tax = float(t.get("tax", 0.0))
        if side == "buy":
            opens[sym].append([float(qty), price, fee])
        else:
            remaining = float(qty)
            sell_unit_cost = (fee + tax) / qty if qty else 0.0
            while remaining > 0 and opens[sym]:
                buy_qty, buy_price, buy_fee = opens[sym][0]
                matched = min(buy_qty, remaining)
                buy_unit_fee = buy_fee / buy_qty if buy_qty else 0.0
                pnl = (price - buy_price) * matched - (buy_unit_fee + sell_unit_cost) * matched
                pnls[sym] += pnl
                counts[sym] += 1
                buy_qty -= matched
                remaining -= matched
                if buy_qty == 0:
                    opens[sym].popleft()
                else:
                    opens[sym][0][0] = buy_qty
                    opens[sym][0][2] = (
                        buy_fee * (buy_qty / (buy_qty + matched)) if (buy_qty + matched) else 0.0
                    )
    out: list[dict[str, Any]] = [
        {"symbol": s, "pnl": pnls[s], "trades": counts[s]} for s in pnls
    ]
    out.sort(key=lambda r: float(r["pnl"]), reverse=True)
    return out


__all__ = [
    "equity_series",
    "list_backtests",
    "list_daily_dates",
    "load_backtest",
    "load_daily",
]
