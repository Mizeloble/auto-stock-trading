"""틱 기반 손절 가드 반사실 — 봉 마감 검사 vs 틱 검사 실측 비교 (read-only).

무엇/왜: 2026-07-01 금호전기(001210) 손절이 설정 -1% 가 아닌 -4.48% 에서 체결
(오버슈트 ≈ 11.8만원) — 원인은 PositionGuard 검사가 1분봉 확정 시점에만 돌아
봉 내부 급락을 놓침. 틱 가드로 바꿀지 판단하려면 "틱 가드였다면 손절가가 얼마였나 /
승자를 조기 손절해 이익을 깎진 않았나"의 실측이 필요하다. PR #157 이 수집하는
`data/ticks/ticks-*.jsonl` 위에서 실제 왕복(daily/shadow 리포트의 round_trips)을
틱 단위로 재생해 두 검사 방식의 손익 차이를 잰다.

방법:
- 실제 왕복의 진입가·진입시각을 그대로 쓰고, 출구만 틱 단위 가드로 교체 시뮬.
  가드 규칙·순서는 PositionGuard 와 동일(고점 갱신 → ①손절 ②트레일링 ③부분익절
  1회) — 파라미터는 daytrade-highvol.yaml 의 risk 값을 읽는다(실운영본).
- 가드 미발화 시 15:15 마지막 틱에 EOD 청산. 비교 대상은 리포트에 기록된 실제
  왕복 pnl_pct(봉 마감 검사 + 실제 체결).
- 보조 지표: 실제 손절(stop_loss) 왕복의 오버슈트 = 실제 pnl - 설정 손절폭.

한계(정직 고지):
- 판정은 왕복 5건·거래일 3일 이상(MIN_TRIPS/MIN_DAYS) — 그 전엔 "판정 보류".
- 틱 가격에 즉시 체결 가정 — 실제 틱 가드도 주문 지연·호가 스프레드로 다소 밀린다
  (시뮬이 틱 가드에 유리한 방향). 슬리피지 왕복 ~0.33% 는 양쪽 공통이라 비교엔 중립.
- 시뮬 고점(트레일링 기준)은 진입가에서 시작 — 실제 가드는 첫 검사 가격에서 시작.
- 가드 파라미터가 그날 프로파일과 다르면(레짐 라우팅) 오차 — 스크립트는 highvol
  고정(현 실운영본), 다른 레짐 날이 섞이면 결과 해석에 주의.

실행: uv run python scripts/diagnostics/tick_guard_study.py
자가 검증(데이터 없이 시뮬 로직 확인): … --selftest
"""

from __future__ import annotations

import json
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from auto_trading.reports.daily import RoundTrip, load_daily_report

TICKS_ROOT = Path("data/ticks")
REPORTS_ROOT = Path("data/reports")
PROFILE_PATH = Path("strategies/profiles/daytrade-highvol.yaml")
EOD_SEC = (15 * 60 + 15) * 60  # 15:15
MIN_TRIPS = 5
MIN_DAYS = 3

# 폴백 기본값 — daytrade-highvol.yaml 을 못 읽을 때만 사용 (2026-07 값).
DEFAULT_GUARD = {
    "stop_loss_pct": 0.010,
    "trailing_stop_pct": 0.008,
    "partial_take_pct": 0.016,
    "partial_take_qty_ratio": 0.5,
}


def load_guard_params() -> dict[str, float]:
    try:
        risk = (yaml.safe_load(PROFILE_PATH.read_text(encoding="utf-8")) or {}).get("risk") or {}
        return {k: float(risk.get(k, v)) for k, v in DEFAULT_GUARD.items()}
    except OSError:
        return dict(DEFAULT_GUARD)


@dataclass(frozen=True)
class SimResult:
    pnl_pct: float  # 가중 손익 % (부분익절 절반 + 잔량)
    exits: list[str]  # 발화 순서 (예: ["partial_take", "trailing"], ["eod"])


def simulate_tick_guard(
    ticks: list[tuple[int, float]],
    *,
    entry_price: float,
    start_sec: int,
    guard: dict[str, float],
) -> SimResult | None:
    """진입 이후 틱을 재생해 PositionGuard 규칙을 틱 단위로 적용.

    ticks: (초, 가격) 시각 오름차순. start_sec~15:15 창의 틱이 없으면 None(결손).
    """
    window = [(s, p) for s, p in ticks if start_sec <= s <= EOD_SEC]
    if not window or entry_price <= 0:
        return None
    highest = entry_price
    remaining = 1.0
    partial_taken = False
    legs: list[tuple[float, float]] = []  # (비중, 청산가)
    exits: list[str] = []
    for _, price in window:
        highest = max(highest, price)
        if 1.0 - price / entry_price >= guard["stop_loss_pct"] > 0:
            legs.append((remaining, price))
            exits.append("stop_loss")
            remaining = 0.0
            break
        if 1.0 - price / highest >= guard["trailing_stop_pct"] > 0:
            legs.append((remaining, price))
            exits.append("trailing")
            remaining = 0.0
            break
        if (
            not partial_taken
            and guard["partial_take_pct"] > 0
            and price / entry_price - 1.0 >= guard["partial_take_pct"]
        ):
            ratio = guard["partial_take_qty_ratio"]
            legs.append((remaining * ratio, price))
            remaining *= 1.0 - ratio
            partial_taken = True
            exits.append("partial_take")
    if remaining > 0:
        legs.append((remaining, window[-1][1]))
        exits.append("eod")
    pnl = sum(frac * (px / entry_price - 1.0) for frac, px in legs) * 100.0
    return SimResult(pnl_pct=pnl, exits=exits)


def load_ticks(date: str) -> dict[str, list[tuple[int, float]]]:
    path = TICKS_ROOT / f"ticks-{date}.jsonl"
    series: dict[str, list[tuple[int, float]]] = {}
    if not path.exists():
        return series
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = str(rec.get("ts") or "")
        symbol = rec.get("symbol")
        price = rec.get("price")
        if len(ts) < 19 or not isinstance(symbol, str) or not isinstance(price, int | float):
            continue
        sec = int(ts[11:13]) * 3600 + int(ts[14:16]) * 60 + int(ts[17:19])
        series.setdefault(symbol, []).append((sec, float(price)))
    for pts in series.values():
        pts.sort(key=lambda t: t[0])
    return series


def load_round_trips(date: str) -> list[RoundTrip]:
    trips: list[RoundTrip] = []
    for prefix in ("daily", "shadow"):
        path = REPORTS_ROOT / f"{prefix}-{date}.json"
        if not path.exists():
            continue
        try:
            trips.extend(load_daily_report(path).round_trips)
        except Exception as exc:
            print(f"⚠️ 로드 실패 스킵: {path} ({exc})")
    return trips


def _hhmm_to_sec(hhmm: str | None) -> int | None:
    if not hhmm or ":" not in hhmm:
        return None
    h, m = hhmm.split(":", 1)
    try:
        return int(h) * 3600 + int(m) * 60
    except ValueError:
        return None


def main() -> None:
    guard = load_guard_params()
    tick_dates = sorted(p.name[6:16] for p in TICKS_ROOT.glob("ticks-*.jsonl"))
    if not tick_dates:
        print(f"데이터 없음: {TICKS_ROOT}/ticks-*.jsonl 이 아직 없습니다.")
        print(
            "TICK_LOG_DIR 활성 + 러너가 유니버스를 구독한 날(거래일 또는 섀도우 차단일)부터"
            " 쌓입니다. 먼저 --selftest 로 시뮬 로직을 확인할 수 있습니다."
        )
        return

    print(
        "=== 틱 가드 반사실 — 봉 마감 검사(실제) vs 틱 검사(시뮬) ===\n"
        f"가드: 손절 {guard['stop_loss_pct']:.1%} · 트레일링 {guard['trailing_stop_pct']:.1%}"
        f" · 부분익절 {guard['partial_take_pct']:.1%}x{guard['partial_take_qty_ratio']:.0%}"
        f" ({PROFILE_PATH.name})"
    )
    deltas: list[float] = []
    overshoots: list[float] = []
    n_days_used = 0
    skipped = 0
    for date in tick_dates:
        trips = load_round_trips(date)
        if not trips:
            continue
        ticks = load_ticks(date)
        day_lines: list[str] = []
        for rt in trips:
            start = _hhmm_to_sec(rt.buy_time_kst)
            if start is None or not rt.buy_price or rt.pnl_pct is None:
                skipped += 1
                continue
            sim = simulate_tick_guard(
                ticks.get(rt.symbol, []),
                entry_price=rt.buy_price,
                start_sec=start,
                guard=guard,
            )
            if sim is None:
                skipped += 1
                continue
            delta = sim.pnl_pct - rt.pnl_pct
            deltas.append(delta)
            if rt.exit_reason == "stop_loss":
                overshoots.append(rt.pnl_pct - (-guard["stop_loss_pct"] * 100.0))
            day_lines.append(
                f"    {rt.symbol}  실제 {rt.pnl_pct:+.2f}% ({rt.exit_reason or '?'})"
                f"  → 틱시뮬 {sim.pnl_pct:+.2f}% ({'→'.join(sim.exits)})  Δ {delta:+.2f}%p"
            )
        if day_lines:
            n_days_used += 1
            print(f"{date}")
            print("\n".join(day_lines))

    if not deltas:
        print(
            f"\n틱 파일 {len(tick_dates)}일은 있으나 겹치는 왕복 기록이 없습니다 (스킵 {skipped})."
        )
        return

    better = sum(1 for d in deltas if d > 0)
    print(
        f"\n--- 종합 ---\n"
        f"왕복 {len(deltas)}건 ({n_days_used}일 · 결손 스킵 {skipped}건):"
        f" 틱시뮬 우위 {better}/{len(deltas)}"
        f" · Δ 평균 {statistics.fmean(deltas):+.2f}%p · Δ 중앙 {statistics.median(deltas):+.2f}%p"
    )
    if overshoots:
        print(
            f"실제 손절 오버슈트: n={len(overshoots)}"
            f" 평균 {statistics.fmean(overshoots):+.2f}%p (0 이면 설정폭 그대로, 음수 = 초과 손실)"
        )
    if len(deltas) < MIN_TRIPS or n_days_used < MIN_DAYS:
        print(
            f"⏳ 판정 보류 — 왕복 {MIN_TRIPS}건·{MIN_DAYS}일 이상 모이면 판정"
            f" (현재 {len(deltas)}건·{n_days_used}일)."
        )
    else:
        print(
            "판정 기준: ① Δ 평균이 양수(틱 가드가 손익 개선)이고 ② 승자 왕복에서 Δ 가 크게"
            " 음수인 건(조기 손절로 이익 훼손)이 소수일 때만 틱 가드 전환 후보."
        )


def selftest() -> None:
    """시뮬 로직 자가 검증 — 합성 틱 3케이스."""
    guard = dict(DEFAULT_GUARD)
    t0 = 9 * 3600 + 10 * 60

    # A. 봉내 폭락(점진): 고점(=진입가) 대비 -0.8% 도달 틱(992)에서 트레일링이 손절보다
    #    먼저 발화 — 실제 가드도 같은 순서다. 봉 마감 검사(-5%)보다 4.2%p 유리.
    crash = [(t0 + 5, 998.0), (t0 + 20, 992.0), (t0 + 35, 990.0), (t0 + 50, 950.0)]
    sim = simulate_tick_guard(crash, entry_price=1000.0, start_sec=t0, guard=guard)
    assert sim is not None and sim.exits == ["trailing"], sim
    assert abs(sim.pnl_pct - (-0.8)) < 1e-9, sim.pnl_pct
    print(f"A 봉내 폭락(점진): {sim.pnl_pct:+.2f}% ({'→'.join(sim.exits)}) — 봉 마감이면 -5.00%")

    # A2. 봉내 폭락(갭): 한 틱에 -1.5% 급락 — 트레일링 전에 손절 조건부터 충족.
    gap = [(t0 + 5, 985.0), (t0 + 20, 950.0)]
    sim = simulate_tick_guard(gap, entry_price=1000.0, start_sec=t0, guard=guard)
    assert sim is not None and sim.exits == ["stop_loss"], sim
    assert abs(sim.pnl_pct - (-1.5)) < 1e-9, sim.pnl_pct
    print(f"A2 봉내 폭락(갭): {sim.pnl_pct:+.2f}% ({'→'.join(sim.exits)}) — 봉 마감이면 -5.00%")

    # B. 승자: +1.6% 부분익절 후 상승 지속 → EOD +3%. 0.5*1.6 + 0.5*3.0 = +2.3%.
    winner = [(t0 + 60, 1005.0), (t0 + 120, 1016.0), (t0 + 180, 1025.0), (EOD_SEC, 1030.0)]
    sim = simulate_tick_guard(winner, entry_price=1000.0, start_sec=t0, guard=guard)
    assert sim is not None and sim.exits == ["partial_take", "eod"], sim
    assert abs(sim.pnl_pct - 2.3) < 1e-9, sim.pnl_pct
    print(f"B 승자 보존: {sim.pnl_pct:+.2f}% ({'→'.join(sim.exits)})")

    # C. 되돌림: 고점 1010 후 -0.8%(1001.92) 도달 틱에서 트레일링.
    whipsaw = [(t0 + 60, 1010.0), (t0 + 120, 1005.0), (t0 + 180, 1001.9)]
    sim = simulate_tick_guard(whipsaw, entry_price=1000.0, start_sec=t0, guard=guard)
    assert sim is not None and sim.exits == ["trailing"], sim
    assert abs(sim.pnl_pct - 0.19) < 1e-9, sim.pnl_pct
    print(f"C 되돌림: {sim.pnl_pct:+.2f}% ({'→'.join(sim.exits)})")

    # D. 결손: 창에 틱 없음 → None.
    assert simulate_tick_guard([], entry_price=1000.0, start_sec=t0, guard=guard) is None
    print("D 틱 결손 → None (결손 처리)")
    print("\n✅ selftest 통과 — 손절·트레일링·부분익절·결손 5케이스 정상.")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()
