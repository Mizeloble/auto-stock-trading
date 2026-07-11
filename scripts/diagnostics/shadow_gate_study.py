"""섀도우 vs 게이트 — 차단일 가상 매매 실적으로 SMA 게이트 완화 판정 (read-only).

무엇/왜: PR #171·#172 이후 게이트 전면 차단일엔 러너가 종료 대신 주문 없는 섀도우
세션으로 돌아 `data/reports/shadow-*.json`(DailyReport 동일 스키마, 가상 PnL)을
남긴다. 이 스크립트는 그 기록을 모아 "게이트가 막은 날 실제 전략 로직(출구 규칙
포함)으로 들어갔다면 얼마였나"를 집계 — 게이트 완화(예: breadth 통과 시 SMA 면제)
가설의 판정 재료다. 스냅샷 buy-hold 반사실은 출구 규칙을 재현 못 해 과대평가
(7/10 금호타이어 +17.5% 가 트레일링에선 +0.75%)라서, 섀도우가 정답지다.

측정:
1. 일별 — 섀도우 실현손익(비용 후), 왕복 수, 출구 사유 분포, 그날 breadth 측정치.
2. 종합 — 평균/합계/흑자일 비율. "게이트 유지 = 0원" 과의 비교가 곧 판정.
3. 출구 효과 — 같은 픽의 buy-hold(스냅샷) 수익률과 섀도우 왕복 pnl 을 병기해
   "원재료 vs 전략 손익" 갭(출구가 자르는 몫)을 정량화.

한계(정직 고지):
- 판정은 섀도우 15일 이상(MIN_DAYS_FOR_VERDICT) — 그 전엔 "판정 보류".
- 섀도우 가정: 즉시 체결 + 고정 슬리피지(SimulatedBroker 동일) — 실체결 잔차 없음.
- 섀도우가 흑자여도 '차단일 평균이 양수' 만으론 부족 — 꼬리 위험(최악일)을 같이 본다.

실행: uv run python scripts/diagnostics/shadow_gate_study.py
자가 검증(데이터 없이 집계 로직 확인): … --selftest
"""

from __future__ import annotations

import statistics
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from auto_trading.reports.daily import DailyReport, RoundTrip, TradeRecord, load_daily_report
from auto_trading.reports.snapshot_reader import parse_topcap_for_date

REPORTS_ROOT = Path("data/reports")
SNAPSHOT_ROOT = Path("data/snapshots")
EXIT_MIN = 15 * 60 + 15
TOL_MIN = 12
MIN_DAYS_FOR_VERDICT = 15


@dataclass(frozen=True)
class DayResult:
    date: str
    pnl_after_cost: float | None
    pnl: float | None
    n_round_trips: int
    exit_reasons: Counter
    breadth_passed: bool | None
    # (심볼, 섀도우 왕복 pnl%, buy-hold%) — 출구 효과 병기용
    trip_vs_hold: list[tuple[str, float | None, float | None]]


def _hhmm_to_minute(hhmm: str | None) -> int | None:
    if not hhmm or ":" not in hhmm:
        return None
    h, m = hhmm.split(":", 1)
    try:
        return int(h) * 60 + int(m)
    except ValueError:
        return None


def buy_hold_pct(report: DailyReport, rt: RoundTrip) -> float | None:
    """왕복의 진입 시각→15:15 buy-hold 수익률(스냅샷 조인). 결손이면 None."""
    start = _hhmm_to_minute(rt.buy_time_kst)
    if start is None or not rt.buy_price:
        return None
    snaps = parse_topcap_for_date(report.date, root=SNAPSHOT_ROOT)
    pts = snaps.series.get(rt.symbol)
    if not pts:
        return None
    nearest = min(pts, key=lambda p: abs(p.minute - EXIT_MIN))
    if abs(nearest.minute - EXIT_MIN) > TOL_MIN:
        return None
    return (nearest.price / rt.buy_price - 1.0) * 100.0


def summarize_day(report: DailyReport) -> DayResult:
    reasons = Counter(rt.exit_reason or "unknown" for rt in report.round_trips)
    trip_vs_hold = [(rt.symbol, rt.pnl_pct, buy_hold_pct(report, rt)) for rt in report.round_trips]
    return DayResult(
        date=report.date,
        pnl_after_cost=report.realized_pnl_after_cost,
        pnl=report.realized_pnl,
        n_round_trips=len(report.round_trips),
        exit_reasons=reasons,
        breadth_passed=report.breadth_passed,
        trip_vs_hold=trip_vs_hold,
    )


def print_study(days: list[DayResult]) -> None:
    print("=== 섀도우 vs 게이트 — 차단일 가상 매매 실적 ===")
    pnls: list[float] = []
    for d in days:
        pnl = d.pnl_after_cost if d.pnl_after_cost is not None else d.pnl
        if pnl is not None:
            pnls.append(pnl)
        pnl_str = f"{pnl:+,.0f}원" if pnl is not None else "결손"
        breadth = {True: "breadth 통과", False: "breadth 차단", None: "breadth ?"}[d.breadth_passed]
        reasons = ", ".join(f"{k}x{v}" for k, v in d.exit_reasons.most_common()) or "-"
        print(f"{d.date}  {pnl_str:>12}  왕복 {d.n_round_trips}  [{breadth}]  출구: {reasons}")
        for sym, trip, hold in d.trip_vs_hold:
            t = f"{trip:+.2f}%" if trip is not None else "?"
            h = f"{hold:+.2f}%" if hold is not None else "?"
            print(f"    {sym}  섀도우 {t}  vs  buy-hold {h}")

    if not pnls:
        print("\nPnL 데이터가 없습니다 (round_trips 없는 리포트뿐).")
        return

    wins = sum(1 for p in pnls if p > 0)
    print(
        f"\n--- 종합 ---\n"
        f"섀도우 {len(pnls)}일: 합계 {sum(pnls):+,.0f}원 · 평균 {statistics.fmean(pnls):+,.0f}원"
        f" · 흑자일 {wins}/{len(pnls)} · 최악일 {min(pnls):+,.0f}원"
    )
    print("게이트 유지 시 같은 날들의 실적 = 0원 (진입 자체가 없음) — 위 수치와 직접 비교.")

    if len(pnls) < MIN_DAYS_FOR_VERDICT:
        print(
            f"⏳ 판정 보류 — 섀도우 {MIN_DAYS_FOR_VERDICT}일 이상 모이면 판정"
            f" (현재 {len(pnls)}일). 위 수치는 방향 참고용."
        )
    else:
        print(
            "판정 기준: ① 평균이 양수이고 ② 흑자일 과반이며 ③ 최악일 손실이 감내 가능"
            "(일일 손실 한도 이내)일 때만 완화 후보. breadth 통과일만 골라 같은 기준을"
            " 재적용하면 'breadth 통과 시 SMA 면제' 가설을 따로 판정할 수 있다."
        )


def load_shadow_days() -> list[DayResult]:
    days: list[DayResult] = []
    for path in sorted(REPORTS_ROOT.glob("shadow-*.json")):
        try:
            report = load_daily_report(path)
        except Exception as exc:  # 손상 파일은 스킵하되 알린다
            print(f"⚠️ 로드 실패 스킵: {path} ({exc})")
            continue
        days.append(summarize_day(report))
    return days


def selftest() -> None:
    """집계 로직 자가 검증 — 파일 없이 합성 리포트 2일치로 출력 형태·수치 확인."""
    mk_rt = lambda sym, pnl_pct, pnl, reason: RoundTrip(  # noqa: E731
        symbol=sym,
        name=None,
        quantity=10,
        buy_price=1000.0,
        sell_price=1000.0 * (1 + pnl_pct / 100),
        buy_time_kst="09:10",
        sell_time_kst="14:00",
        holding_minutes=290,
        pnl=pnl,
        pnl_pct=pnl_pct,
        exit_reason=reason,
    )
    base = dict(
        mode="day-trade",
        dry_run=True,
        discovery_chosen=[],
        discovery_blocked=[("005930", "KOSPI_below_sma")],
        market_gates=[],
        liquidations=[],
        error_count=0,
        note_ko="셀프테스트",
        buys=[TradeRecord(symbol="005930", side="buy", quantity=10, price=1000.0, simulated=True)],
    )
    days = [
        summarize_day(
            DailyReport(
                date="2026-07-13",
                realized_pnl=12_000.0,
                realized_pnl_after_cost=10_000.0,
                round_trips=[mk_rt("005930", 1.2, 12_000.0, "trailing")],
                breadth_passed=True,
                **base,  # type: ignore[arg-type]
            )
        ),
        summarize_day(
            DailyReport(
                date="2026-07-14",
                realized_pnl=-6_000.0,
                realized_pnl_after_cost=-8_000.0,
                round_trips=[mk_rt("005930", -0.6, -6_000.0, "stop_loss")],
                breadth_passed=False,
                **base,  # type: ignore[arg-type]
            )
        ),
    ]
    print_study(days)
    total = sum(d.pnl_after_cost for d in days if d.pnl_after_cost is not None)
    assert total == 2_000.0, total
    assert days[0].exit_reasons == Counter({"trailing": 1})
    print("\n✅ selftest 통과 — 합계 +2,000원, 출구 사유 집계 정상.")


def main() -> None:
    if "--selftest" in sys.argv:
        selftest()
        return
    days = load_shadow_days()
    if not days:
        print(f"데이터 없음: {REPORTS_ROOT}/shadow-*.json 이 아직 없습니다.")
        print(
            "다음 게이트 차단일(섀도우 활성 프로파일)부터 하루 1개씩 생깁니다 (PR #172)."
            " 먼저 --selftest 로 집계 로직을 확인할 수 있습니다."
        )
        return
    print_study(days)


if __name__ == "__main__":
    main()
