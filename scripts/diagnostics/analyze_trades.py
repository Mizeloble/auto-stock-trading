"""거래 단위 원장 재구성 — 봇이 라운드트립별로 어떻게 벌고 잃는지 해부.

무엇/왜:
- 일일 리포트(data/reports/daily-*.json)는 손익·비용 '합계'만 보여준다. 수익률이 나쁜
  진짜 원인(과매매·비용)을 보려면 매수↔매도를 1:1로 묶은 '라운드트립' 단위가 필요하다.
- 원본 로그(logs/paper-day-*.log)에는 타임스탬프·청산사유까지 있어 보유시간·청산 패턴을
  복원할 수 있다. 파싱은 reports/log_parser.py(LogEvents) 한 곳을 재사용(중복 파서 금지).
- 비용 상수는 reports/daily.py 의 값(왕복 ~0.33%)을 그대로 import — 리포트와 일치 보장.

어떻게 확인: 스크립트가 끝에 '교차검증'을 출력 — 라운드트립 손익 합이 리포트 realized_pnl
합과, 추정 비용 합이 리포트 total_fees+taxes+slippage 합과 맞는지 비교한다.

실행: uv run python scripts/diagnostics/analyze_trades.py
"""

from __future__ import annotations

import glob
import json
import os
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from auto_trading.reports.daily import (
    COST_COMMISSION_RATE,
    COST_SLIPPAGE_BPS,
    COST_TAX_RATE_SELL,
)
from auto_trading.reports.log_parser import parse_log

SLIP_RATE = COST_SLIPPAGE_BPS / 10_000.0  # bps → 비율
LOG_GLOB = "logs/paper-day-*.log"
REPORT_GLOB = "data/reports/daily-*.json"


def _ts(record: dict) -> datetime | None:
    raw = record.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class Fill:
    ts: datetime | None
    symbol: str
    side: str  # "buy" | "sell"
    qty: float
    price: float | None
    is_liq: bool = False


@dataclass
class RoundTrip:
    date: str
    symbol: str
    entry_ts: datetime | None
    exit_ts: datetime | None
    qty: float
    buy_price: float
    sell_price: float
    is_liq: bool

    @property
    def gross_pnl(self) -> float:
        return (self.sell_price - self.buy_price) * self.qty

    @property
    def hold_min(self) -> float | None:
        if self.entry_ts is None or self.exit_ts is None:
            return None
        return (self.exit_ts - self.entry_ts).total_seconds() / 60.0

    @property
    def cost(self) -> float:
        buy_notional = self.buy_price * self.qty
        sell_notional = self.sell_price * self.qty
        buy_leg = buy_notional * (COST_COMMISSION_RATE + SLIP_RATE)
        sell_leg = sell_notional * (COST_COMMISSION_RATE + COST_TAX_RATE_SELL + SLIP_RATE)
        return buy_leg + sell_leg

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.cost


@dataclass
class DayLedger:
    date: str
    fills: list[Fill] = field(default_factory=list)
    partial_takes: int = 0
    stop_losses: int = 0
    trailing_stops: int = 0
    take_profits: int = 0


def _fills_from_log(path: Path) -> tuple[list[Fill], DayLedger]:
    events = parse_log(path)
    date = path.name.replace("paper-day-", "").replace(".log", "")
    ledger = DayLedger(date=date)
    ledger.partial_takes = len(events.partial_takes)

    fills: list[Fill] = []
    for rec in (*events.orders_accepted, *events.orders_simulated):
        side = str(rec.get("side", "")).lower()
        if side not in ("buy", "sell"):
            continue
        price = rec.get("price")
        fills.append(
            Fill(
                ts=_ts(rec),
                symbol=str(rec.get("symbol", "")),
                side=side,
                qty=float(rec.get("qty", rec.get("quantity", 0)) or 0),
                price=float(price) if isinstance(price, (int, float)) else None,
            )
        )
    for rec in (*events.liquidations_accepted, *events.liquidations_simulated):
        price = rec.get("sell_price", rec.get("price"))
        fills.append(
            Fill(
                ts=_ts(rec),
                symbol=str(rec.get("symbol", "")),
                side="sell",
                qty=float(rec.get("qty", rec.get("quantity", 0)) or 0),
                price=float(price) if isinstance(price, (int, float)) else None,
                is_liq=True,
            )
        )
    fills.sort(key=lambda f: f.ts or datetime.min.replace(tzinfo=None) if f.ts is None else f.ts)
    return fills, ledger


def _scan_exit_reasons(path: Path, ledger: DayLedger) -> None:
    """log_parser 가 안 잡는 position_guard.{stop_loss,trailing,take_profit} 직접 카운트."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line[0] != "{":
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ev = rec.get("event")
                if ev == "position_guard.stop_loss":
                    ledger.stop_losses += 1
                elif ev == "position_guard.trailing":
                    ledger.trailing_stops += 1
                elif ev == "position_guard.take_profit":
                    ledger.take_profits += 1
    except OSError:
        pass


def _build_round_trips(date: str, fills: list[Fill]) -> list[RoundTrip]:
    """종목별 FIFO 로 매수↔매도/청산을 묶어 라운드트립 생성.

    매수가·매도가가 둘 다 있는 분량만 손익에 포함(reports/daily._aggregate_realized 와 동일
    원칙). 한쪽 가격이 없으면 재고만 소진하고 손익 미산정.
    """
    queues: dict[str, list[list]] = {}  # symbol -> [[qty, price, ts], ...]
    trips: list[RoundTrip] = []
    for f in fills:
        if f.side == "buy":
            if f.qty > 0:
                queues.setdefault(f.symbol, []).append([f.qty, f.price, f.ts])
            continue
        # sell / liquidation: FIFO 소진
        remaining = f.qty
        q = queues.get(f.symbol) or []
        matched_qty = 0.0
        weighted_buy = 0.0
        entry_ts = None
        while remaining > 0 and q:
            lot = q[0]
            lot_qty, buy_price, buy_ts = lot[0], lot[1], lot[2]
            take = min(remaining, lot_qty)
            if buy_price is not None and f.price is not None:
                matched_qty += take
                weighted_buy += buy_price * take
                if entry_ts is None:
                    entry_ts = buy_ts  # FIFO → 가장 오래된 로트 = 보유시간 상한
            remaining -= take
            lot[0] = lot_qty - take
            if lot[0] <= 0:
                q.pop(0)
        if matched_qty > 0 and f.price is not None:
            trips.append(
                RoundTrip(
                    date=date,
                    symbol=f.symbol,
                    entry_ts=entry_ts,
                    exit_ts=f.ts,
                    qty=matched_qty,
                    buy_price=weighted_buy / matched_qty,
                    sell_price=f.price,
                    is_liq=f.is_liq,
                )
            )
    return trips


def _report_totals() -> tuple[float, float, dict[str, dict]]:
    """리포트 합계(교차검증 기준): realized_pnl 합, 비용 합, 날짜별 요약."""
    realized_sum = 0.0
    cost_sum = 0.0
    by_date: dict[str, dict] = {}
    for f in sorted(glob.glob(REPORT_GLOB)):
        with open(f) as fh:
            d = json.load(fh)
        date = os.path.basename(f)[6:16]
        rp = d.get("realized_pnl")
        fees = d.get("total_fees") or 0.0
        tax = d.get("total_taxes") or 0.0
        slip = d.get("total_slippage") or 0.0
        if isinstance(rp, (int, float)):
            realized_sum += rp
        cost_sum += fees + tax + slip
        by_date[date] = {
            "realized_pnl": rp,
            "after_cost": d.get("realized_pnl_after_cost"),
            "turnover": d.get("turnover_ratio"),
            "fees": fees,
            "tax": tax,
            "slip": slip,
        }
    return realized_sum, cost_sum, by_date


def _pct(n: float, d: float) -> str:
    return f"{n / d * 100:.1f}%" if d else "—"


def main() -> None:
    log_paths = sorted(Path(p) for p in glob.glob(LOG_GLOB))
    all_trips: list[RoundTrip] = []
    ledgers: list[DayLedger] = []
    for path in log_paths:
        fills, ledger = _fills_from_log(path)
        _scan_exit_reasons(path, ledger)
        ledger.fills = fills
        ledgers.append(ledger)
        all_trips.extend(_build_round_trips(ledger.date, fills))

    # ---- 라운드트립 통계 ----
    wins = [t for t in all_trips if t.gross_pnl > 0]
    losses = [t for t in all_trips if t.gross_pnl < 0]
    gross = sum(t.gross_pnl for t in all_trips)
    cost = sum(t.cost for t in all_trips)
    net = sum(t.net_pnl for t in all_trips)
    holds = [t.hold_min for t in all_trips if t.hold_min is not None]
    notionals = [t.sell_price * t.qty for t in all_trips]

    print("=" * 72)
    print("거래 단위 원장 (라운드트립) — 봇이 어떻게 벌고 잃는가")
    print("=" * 72)
    print(f"라운드트립 수      : {len(all_trips)}  (승 {len(wins)} / 패 {len(losses)})")
    print(f"승률               : {_pct(len(wins), len(all_trips))}")
    if wins:
        print(f"평균 이익(총)      : {statistics.mean(t.gross_pnl for t in wins):>12,.0f}원")
    if losses:
        print(f"평균 손실(총)      : {statistics.mean(t.gross_pnl for t in losses):>12,.0f}원")
    print(f"총손익(비용 전)    : {gross:>12,.0f}원")
    print(f"추정 비용          : {-cost:>12,.0f}원   (총이익 대비 {_pct(cost, gross)})")
    print(f"순손익(비용 후)    : {net:>12,.0f}원")
    if all_trips:
        print(
            f"라운드트립당 기대값: 총 {gross / len(all_trips):>8,.0f}원 / 순 {net / len(all_trips):>8,.0f}원"
        )
    if holds:
        print(
            f"보유시간(분)       : 중앙 {statistics.median(holds):.0f} / 평균 "
            f"{statistics.mean(holds):.0f} / 최소 {min(holds):.0f} / 최대 {max(holds):.0f}"
        )
    if notionals:
        print(
            f"거래대금/트립(원)  : 중앙 {statistics.median(notionals):,.0f} / 평균 "
            f"{statistics.mean(notionals):,.0f}"
        )

    # ---- 청산 이벤트 패턴 (거래세 주범) ----
    print("\n" + "-" * 72)
    print("청산 이벤트 패턴 (매도/청산 1건마다 거래세 0.20% 발생)")
    print("-" * 72)
    tot_partial = sum(lg.partial_takes for lg in ledgers)
    tot_stop = sum(lg.stop_losses for lg in ledgers)
    tot_trail = sum(lg.trailing_stops for lg in ledgers)
    tot_take = sum(lg.take_profits for lg in ledgers)
    sell_trips = len([t for t in all_trips if not t.is_liq])
    liq_trips = len([t for t in all_trips if t.is_liq])
    print(f"장중 매도 트립     : {sell_trips}")
    print(f"EOD 청산 트립      : {liq_trips}")
    print(f"부분익절 이벤트    : {tot_partial}  (포지션당 매도를 늘려 과세 횟수↑)")
    print(f"손절(stop_loss)    : {tot_stop}")
    print(f"트레일링(trailing) : {tot_trail}")
    print(f"익절(take_profit)  : {tot_take}")

    # ---- 일자별 ----
    realized_sum, report_cost_sum, by_date = _report_totals()
    print("\n" + "-" * 72)
    print("일자별 (원장 vs 리포트)")
    print("-" * 72)
    print(f"{'date':12}{'트립':>4}{'총손익':>11}{'비용':>10}{'순손익':>11}{'회전율':>8}")
    trips_by_date: dict[str, list[RoundTrip]] = {}
    for t in all_trips:
        trips_by_date.setdefault(t.date, []).append(t)
    for date in sorted(by_date):
        dts = trips_by_date.get(date, [])
        if not dts and by_date[date]["realized_pnl"] in (None, 0):
            continue
        g = sum(t.gross_pnl for t in dts)
        c = sum(t.cost for t in dts)
        turn = by_date[date]["turnover"]
        turns = f"{turn * 100:.1f}%" if isinstance(turn, (int, float)) else "—"
        print(f"{date:12}{len(dts):>4}{g:>11,.0f}{-c:>10,.0f}{g - c:>11,.0f}{turns:>8}")

    # ---- 교차검증 ----
    print("\n" + "=" * 72)
    print("교차검증 (원장 재구성이 리포트와 맞는가)")
    print("=" * 72)
    print(f"원장 총손익 합     : {gross:>12,.0f}원")
    print(f"리포트 realized 합 : {realized_sum:>12,.0f}원")
    print(f"  차이             : {gross - realized_sum:>12,.0f}원")
    print(f"원장 추정비용 합   : {cost:>12,.0f}원")
    print(f"리포트 비용 합     : {report_cost_sum:>12,.0f}원")
    print(f"  차이             : {cost - report_cost_sum:>12,.0f}원")


if __name__ == "__main__":
    main()
