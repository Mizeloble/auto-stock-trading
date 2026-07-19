"""ETF 풀 편입 반사실 검증 — 2026-07-19 사용자 재검토 요청 (read-only).

무엇/왜: 발굴 풀에서 ETF 를 이름 휴리스틱으로 제외 중(exclude_etf=True,
  probe 2026-05-03 실측 후 도입 — 거래량 상위가 KODEX 인버스/ETN/스팩 위주).
  "ETF 를 넣으면 어떤가"를 실데이터로 재검토한다.

방법:
- 과거 어떤 ETF 가 풀 몇 위였는지는 로그에 없음(excluded_count 만 기록).
  대신 거래량 상위를 상시 지배하는 초고유동성 ETF 바스켓(레버리지·인버스·
  대표지수 7종)을 사용 — ETF 허용 시 이들이 사실상 매일 풀 상위였을 것.
- 각 ETF·각 거래일에 현행 전략 규칙 그대로 시뮬:
  진입 = 09:10~14:30 중 "15분 수익률 ≥ +0.35%"(highvol momentum_threshold)
  첫 충족 분봉, 출구 = PositionGuard 동일 스택(손절 1.0%·트레일링 0.8%·
  부분익절 1.6%·15:15 청산), 비용 = 주식 모델(0.33%) 과 ETF 모델(매도
  거래세 0.20% 면제 → +0.20%p 보정 근사) 병기.
- 비교: 봇이 실제 거래한 날(highvol 시대)의 실제 픽 성적 vs 같은 날 ETF.

한계(정직 고지):
- ETF 순위 편입 위치는 재구성 불가 — "바스켓이 풀 상위" 가정. 분봉 종가 체결
  근사. ETF 비용 보정은 매도가≈매수가 근사(오차 < 0.01%p).
- 인버스 ETF 의 "게이트 차단일 역방향 베팅"은 별도 3년 검증에서 이미 기각
  (inverse_etf_study, 당일 방향 랜덤·비용 차감 음) — 본 스터디는 "모멘텀 풀
  편입" 관점만 다룬다.

실행: uv run python scripts/diagnostics/etf_pool_study.py
"""

from __future__ import annotations

import asyncio
import json
import statistics
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path

from entry_axes_study import (
    GuardParams,
    fetch_minute_closes,
    simulate_entry,
)

from auto_trading.kiwoom.rest import KiwoomRestClient
from auto_trading.reports.daily import COST_TAX_RATE_SELL

# 거래량 상위 상시 점유 ETF 바스켓 (probe 2026-05-03 계열 + 대표지수)
BASKET = {
    "122630": "KODEX 레버리지",
    "252670": "KODEX 200선물인버스2X",
    "114800": "KODEX 인버스",
    "233740": "KODEX 코스닥150레버리지",
    "251340": "KODEX 코스닥150선물인버스",
    "069500": "KODEX 200",
    "229200": "KODEX 코스닥150",
}
OLDEST = date(2026, 6, 2)
PARAMS = GuardParams(stop=0.010, trail=0.008, partial=0.016, partial_ratio=0.5)  # highvol
MOMENTUM_PCT = 0.35  # highvol momentum_threshold_pct
LOOKBACK_MIN = 15
ENTRY_FROM = time(9, 10)
ENTRY_UNTIL = time(14, 30)
POSITION_NOTIONAL = 3_360_000.0  # 계좌 ~48M x 7% — 실제 픽과 동일 사이즈 가정
REPORT_DIR = Path("data/reports")
OUT_PATH = Path("data/studies/etf-pool-study.json")
ETF_TAX_REBATE_PCT = COST_TAX_RATE_SELL * 100.0  # 매도 거래세 면제 근사 보정(+%p)


@dataclass
class EtfTrade:
    day: date
    symbol: str
    entry_hhmm: str
    exit_reason: str
    net_pct_stock_cost: float
    net_pct_etf_cost: float


def momentum_entry_time(bars: list[tuple[datetime, float]]) -> time | None:
    """09:10~14:30 중 15분 수익률 ≥ +0.35% 첫 충족 분봉 시각 (전략 진입 규칙 재현)."""
    for idx, (ts, px) in enumerate(bars):
        if ts.time() < ENTRY_FROM:
            continue
        if ts.time() > ENTRY_UNTIL:
            return None
        base = None
        for prev_ts, prev_px in bars[max(0, idx - LOOKBACK_MIN) : idx]:
            if (ts - prev_ts).total_seconds() <= LOOKBACK_MIN * 60:
                base = prev_px
                break
        if base and base > 0 and (px / base - 1.0) * 100.0 >= MOMENTUM_PCT:
            return ts.time()
    return None


def bot_traded_days() -> dict[date, float]:
    """highvol 시대 봇이 매수한 날 → 그날 비용차감 순손익 (daily/shadow 리포트)."""
    days: dict[date, float] = {}
    for path in sorted(REPORT_DIR.glob("*-2026-*.json")):
        data = json.loads(path.read_text())
        day_str = data.get("date")
        if not day_str or date.fromisoformat(day_str) < date(2026, 6, 15):
            continue
        if not data.get("buys"):
            continue
        pnl = data.get("realized_pnl_after_cost")
        if isinstance(pnl, (int, float)):
            days[date.fromisoformat(day_str)] = float(pnl)
    return days


async def main() -> None:
    bars_by_symbol: dict[str, dict[date, list[tuple[datetime, float]]]] = {}
    async with KiwoomRestClient() as client:
        for symbol in BASKET:
            bars_by_symbol[symbol] = await fetch_minute_closes(client, symbol, oldest_day=OLDEST)

    trades: list[EtfTrade] = []
    no_signal_days: dict[str, int] = dict.fromkeys(BASKET, 0)
    frozen_days: dict[str, int] = dict.fromkeys(BASKET, 0)
    for symbol, by_day in bars_by_symbol.items():
        for day, bars in sorted(by_day.items()):
            prices = {px for _, px in bars}
            if len(prices) <= 1:  # 휴장일 스테일(예: 07-17) 배제
                frozen_days[symbol] += 1
                continue
            entry_at = momentum_entry_time(bars)
            if entry_at is None:
                no_signal_days[symbol] += 1
                continue
            sim = simulate_entry(bars, entry_at=entry_at, params=PARAMS)
            if sim is None:
                no_signal_days[symbol] += 1
                continue
            trades.append(
                EtfTrade(
                    day=day,
                    symbol=symbol,
                    entry_hhmm=sim.entry_ts.strftime("%H:%M"),
                    exit_reason=sim.exit_reason,
                    net_pct_stock_cost=sim.net_return_pct,
                    net_pct_etf_cost=sim.net_return_pct + ETF_TAX_REBATE_PCT,
                )
            )

    print("=" * 76)
    print("ETF 풀 편입 반사실 — 현행 전략 규칙(모멘텀 진입 + 출구 스택) 그대로 적용")
    print(f"바스켓 {len(BASKET)}종 · 기간 {OLDEST} 이후 · 비용: 주식 모델 / ETF 모델 병기")
    print("=" * 76)
    print(
        f"{'ETF':<22}{'거래':>5}{'무신호':>7}{'평균순익(주식비용)':>18}{'평균순익(ETF비용)':>18}{'승률':>7}"
    )
    per_symbol: dict[str, dict] = {}
    for symbol, name in BASKET.items():
        rows = [t for t in trades if t.symbol == symbol]
        if not rows:
            print(f"{name:<22}{0:>5}{no_signal_days[symbol]:>7}")
            continue
        mean_stock = statistics.mean(t.net_pct_stock_cost for t in rows)
        mean_etf = statistics.mean(t.net_pct_etf_cost for t in rows)
        wins = sum(1 for t in rows if t.net_pct_etf_cost > 0)
        per_symbol[symbol] = {
            "name": name,
            "trades": len(rows),
            "no_signal_days": no_signal_days[symbol],
            "mean_net_pct_stock_cost": mean_stock,
            "mean_net_pct_etf_cost": mean_etf,
            "win_rate": wins / len(rows),
        }
        print(
            f"{name:<22}{len(rows):>5}{no_signal_days[symbol]:>7}"
            f"{mean_stock:>+17.2f}%{mean_etf:>+17.2f}%{wins / len(rows):>6.0%}"
        )

    all_etf = [t.net_pct_etf_cost for t in trades]
    summary: dict = {"basket_days": {}, "verdict": None}
    if all_etf:
        mean_all = statistics.mean(all_etf)
        med_all = statistics.median(all_etf)
        win_all = sum(1 for v in all_etf if v > 0) / len(all_etf)
        won_all = mean_all / 100.0 * POSITION_NOTIONAL
        print("-" * 76)
        print(
            f"ETF 전체: {len(all_etf)}거래 · 평균 {mean_all:+.2f}% (중앙값 {med_all:+.2f}%,"
            f" 승률 {win_all:.0%}) · 포지션 336만원 기준 건당 {won_all:+,.0f}원"
        )
        summary["etf_all"] = {
            "trades": len(all_etf),
            "mean_net_pct": mean_all,
            "median_net_pct": med_all,
            "win_rate": win_all,
            "mean_won_per_trade": won_all,
        }

    # 봇 실거래일 head-to-head: 같은 날 ETF 상위 3(모멘텀 시각 빠른 순) vs 실제 픽
    traded = bot_traded_days()
    print("-" * 76)
    print("봇 거래일 head-to-head (ETF 는 모멘텀 첫 충족 3종, 336만원/종 가정):")
    h2h: list[dict] = []
    for day, actual_pnl in sorted(traded.items()):
        day_trades = sorted((t for t in trades if t.day == day), key=lambda t: t.entry_hhmm)[:3]
        etf_won = sum(t.net_pct_etf_cost / 100.0 * POSITION_NOTIONAL for t in day_trades)
        h2h.append(
            {
                "day": day.isoformat(),
                "actual_pnl": actual_pnl,
                "etf_top3_pnl": etf_won,
                "etf_trades": len(day_trades),
            }
        )
        print(
            f"  {day} 실제픽 {actual_pnl:>+10,.0f}원 | ETF top3 {etf_won:>+10,.0f}원"
            f" ({len(day_trades)}건)"
        )
    if h2h:
        total_actual = sum(r["actual_pnl"] for r in h2h)
        total_etf = sum(r["etf_top3_pnl"] for r in h2h)
        print(f"  합계     실제픽 {total_actual:>+10,.0f}원 | ETF top3 {total_etf:>+10,.0f}원")
        summary["head_to_head"] = {
            "days": len(h2h),
            "actual_total": total_actual,
            "etf_total": total_etf,
        }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(
            {
                "study": "etf-pool",
                "basket": BASKET,
                "oldest": OLDEST.isoformat(),
                "per_symbol": per_symbol,
                "summary": summary,
                "head_to_head": h2h,
                "trades": [
                    {
                        "day": t.day.isoformat(),
                        "symbol": t.symbol,
                        "entry": t.entry_hhmm,
                        "exit_reason": t.exit_reason,
                        "net_pct_etf_cost": round(t.net_pct_etf_cost, 3),
                    }
                    for t in trades
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"저장: {OUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
