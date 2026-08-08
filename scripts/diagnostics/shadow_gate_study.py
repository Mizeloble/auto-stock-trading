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

표본 산정(2026-08-03 수정):
- 진입 0회인 날도 **0원 관측치로 센다** — 게이트를 완화했어도 그날 성과는 0원이었다는
  사실 자체가 판정 재료다(빼면 차단일당 기대값이 과대평가된다).
- 단, "안 산 날"과 "못 본 날"은 다르다. 봇이 늦게 떠서 아침 장을 통째로 놓친 날은
  전략이 고르지 않은 게 아니라 물어보지도 못한 것이라 표본에서 제외한다. 판별은
  타임라인 첫 이벤트 시각(SESSION_START_MAX_MIN) — 정상 가동일은 09:02 에 시작한다.
  실제 사례: 2026-07-30 은 첫 이벤트 13:07 + 로그에 1시간 간격 공백(맥북 슬립 사고,
  7/28~30 데이터 무효)이라 제외, 2026-07-17 은 09:02~15:15 정상 가동 후 시장 폭
  게이트가 후보 10건을 전부 막은 진짜 무진입일이라 0원으로 포함.

한계(정직 고지):
- 판정은 섀도우 15일 이상(MIN_DAYS_FOR_VERDICT) — 그 전엔 "판정 보류".
- 섀도우 가정: 즉시 체결 + 고정 슬리피지(SimulatedBroker 동일) — 실체결 잔차 없음.
- 섀도우가 흑자여도 '차단일 평균이 양수' 만으론 부족 — 꼬리 위험(최악일)을 같이 본다.
- 0원 관측일은 흑자일 비율의 분모에 들어가되 분자엔 안 들어간다(0 은 흑자가 아님).
  게이트 유지 대비로는 무승부라 보수적인 쪽 — 판정을 완화 방향으로 밀지 않는다.

실행: uv run python scripts/diagnostics/shadow_gate_study.py
자가 검증(데이터 없이 집계 로직 확인): … --selftest
"""

from __future__ import annotations

import statistics
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from auto_trading.reports.daily import (
    DailyReport,
    RoundTrip,
    TimelineEvent,
    TradeRecord,
    load_daily_report,
)
from auto_trading.reports.snapshot_reader import parse_topcap_for_date

REPORTS_ROOT = Path("data/reports")
SNAPSHOT_ROOT = Path("data/snapshots")
EXIT_MIN = 15 * 60 + 15
TOL_MIN = 12
MIN_DAYS_FOR_VERDICT = 15
# 정상 가동일의 첫 타임라인 이벤트는 09:02(발굴). 이보다 늦게 시작했으면 아침 장을
# 놓친 것 — 진입 0회여도 "안 샀다"가 아니라 "못 봤다"라 표본에서 뺀다.
SESSION_START_MAX_MIN = 9 * 60 + 10


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
    first_event_kst: str | None  # 타임라인 첫 이벤트 "HH:MM" — 가동 커버리지 판별용

    @property
    def session_covered_open(self) -> bool:
        """개장 구간을 실제로 관측했나 — 진입 0회를 0원으로 셀 자격."""
        minute = _hhmm_to_minute(self.first_event_kst)
        return minute is not None and minute <= SESSION_START_MAX_MIN


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
        first_event_kst=report.timeline[0].time_kst if report.timeline else None,
    )


def counted_pnl(d: DayResult) -> float | None:
    """표본에 넣을 손익. 진입 0회여도 개장을 관측했으면 0원, 못 봤으면 None(제외)."""
    pnl = d.pnl_after_cost if d.pnl_after_cost is not None else d.pnl
    if pnl is not None:
        return pnl
    return 0.0 if d.session_covered_open else None


def print_study(days: list[DayResult]) -> None:
    print("=== 섀도우 vs 게이트 — 차단일 가상 매매 실적 ===")
    pnls: list[float] = []
    dropped: list[DayResult] = []
    n_zero_days = 0
    for d in days:
        pnl = d.pnl_after_cost if d.pnl_after_cost is not None else d.pnl
        counted = counted_pnl(d)
        if counted is not None:
            pnls.append(counted)
        if pnl is not None:
            pnl_str = f"{pnl:+,.0f}원"
        elif counted is not None:
            pnl_str = "0원(무진입)"
            n_zero_days += 1
        else:
            pnl_str = "표본제외"
            dropped.append(d)
        breadth = {True: "breadth 통과", False: "breadth 차단", None: "breadth ?"}[d.breadth_passed]
        reasons = ", ".join(f"{k}x{v}" for k, v in d.exit_reasons.most_common()) or "-"
        print(f"{d.date}  {pnl_str:>12}  왕복 {d.n_round_trips}  [{breadth}]  출구: {reasons}")
        for sym, trip, hold in d.trip_vs_hold:
            t = f"{trip:+.2f}%" if trip is not None else "?"
            h = f"{hold:+.2f}%" if hold is not None else "?"
            print(f"    {sym}  섀도우 {t}  vs  buy-hold {h}")

    # 무엇을 뺐는지 반드시 드러낸다 — 조용한 표본 축소는 "다 셌다"로 오독된다.
    for d in dropped:
        first = d.first_event_kst or "이벤트 없음"
        print(
            f"⚠️ {d.date} 표본제외 — 첫 이벤트 {first} (정상 가동일은 09:02)."
            " 아침 장을 못 봐서 '안 샀다'가 아니라 '못 봤다'."
        )

    if not pnls:
        print("\nPnL 데이터가 없습니다 (round_trips 없는 리포트뿐).")
        return

    wins = sum(1 for p in pnls if p > 0)
    zero_note = f" (그중 무진입 0원 {n_zero_days}일)" if n_zero_days else ""
    print(
        f"\n--- 종합 ---\n"
        f"섀도우 {len(pnls)}일{zero_note}: 합계 {sum(pnls):+,.0f}원"
        f" · 평균 {statistics.fmean(pnls):+,.0f}원"
        f" · 흑자일 {wins}/{len(pnls)} · 최악일 {min(pnls):+,.0f}원"
    )
    print("게이트 유지 시 같은 날들의 실적 = 0원 (진입 자체가 없음) — 위 수치와 직접 비교.")

    # 완화 가설의 실제 형태는 전면 면제가 아니라 "breadth 통과일만 SMA 면제" —
    # 그 소그룹을 같은 기준으로 따로 집계해야 판정이 한 번에 끝난다.
    b_pnls = [p for d in days if d.breadth_passed is True if (p := counted_pnl(d)) is not None]
    if b_pnls:
        b_wins = sum(1 for p in b_pnls if p > 0)
        print(
            f"breadth 통과일 {len(b_pnls)}일: 합계 {sum(b_pnls):+,.0f}원"
            f" · 평균 {statistics.fmean(b_pnls):+,.0f}원"
            f" · 흑자일 {b_wins}/{len(b_pnls)} · 최악일 {min(b_pnls):+,.0f}원"
            " — 'breadth 통과 시 SMA 면제' 가설은 이 줄로 판정."
        )

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
    tl = lambda hhmm: [TimelineEvent(time_kst=hhmm, kind="discovery", text="발굴 채택 0건")]  # noqa: E731
    days = [
        summarize_day(
            DailyReport(
                date="2026-07-13",
                realized_pnl=12_000.0,
                realized_pnl_after_cost=10_000.0,
                round_trips=[mk_rt("005930", 1.2, 12_000.0, "trailing")],
                breadth_passed=True,
                timeline=tl("09:02"),
                **base,
            )
        ),
        summarize_day(
            DailyReport(
                date="2026-07-14",
                realized_pnl=-6_000.0,
                realized_pnl_after_cost=-8_000.0,
                round_trips=[mk_rt("005930", -0.6, -6_000.0, "stop_loss")],
                breadth_passed=False,
                timeline=tl("09:02"),
                **base,
            )
        ),
        # 정상 가동 + 진입 0회 → 0원 관측치로 포함.
        summarize_day(
            DailyReport(
                date="2026-07-17",
                realized_pnl=None,
                realized_pnl_after_cost=None,
                round_trips=[],
                breadth_passed=False,
                timeline=tl("09:02"),
                **base,
            )
        ),
        # 늦게 뜬 날(아침 장 못 봄) + 진입 0회 → 표본 제외.
        summarize_day(
            DailyReport(
                date="2026-07-30",
                realized_pnl=None,
                realized_pnl_after_cost=None,
                round_trips=[],
                breadth_passed=True,
                timeline=tl("13:07"),
                **base,
            )
        ),
    ]
    print_study(days)
    total = sum(d.pnl_after_cost for d in days if d.pnl_after_cost is not None)
    assert total == 2_000.0, total
    assert days[0].exit_reasons == Counter({"trailing": 1})
    counted = [counted_pnl(d) for d in days]
    assert counted == [10_000.0, -8_000.0, 0.0, None], counted
    assert [d.session_covered_open for d in days] == [True, True, True, False]
    print("\n✅ selftest 통과 — 합계 +2,000원, 무진입일 0원 포함·늦은 시작일 제외 정상.")


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
