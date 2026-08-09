"""발굴 신호 A/B (N축) — 거래량 순위 vs 대안 순위 원재료 수익률 비교 (read-only).

무엇/왜: 거래대금 A/B(`alt_rank_ab_study.py`, 2026-07-31 기각)는 두 풀이 크게 겹쳐
검정력이 안 나왔다. 그 후속으로 `data/discovery/alt-rank.jsonl` v2 가 축을 여러 개
동시에 축적하기 시작했고(외인/기관 순매수 — 겹침 실측 13%), 이 스크립트는 축이
몇 개든 같은 방법으로 한 번에 판정한다. 방법 자체는 selection_alpha_study →
alt_rank_ab_study 계보를 그대로 잇는다(창 09:10→15:15 buy-and-hold, KOSPI 통제).

사전 등록(중요 — 사후 축 고르기 방지):
- **1차 가설은 `foreign_netbuy`(외인 순매수) 하나.** 판정은 이 축으로만 내린다.
- `institution_netbuy` 는 탐색용 2차, `value`(거래대금)는 이미 기각된 대조군.
  둘은 참고 출력이며, 여기서 나온 수치로 "그럼 이 축으로 바꾸자"는 결론을 내지 않는다
  (축을 여러 개 보면 우연히 좋은 축이 하나는 나오게 돼 있다 — 다중비교).
- 판정 조건 3종은 아래 `_verdict_lines()` 에 박아 두고 표본 15일에 자동 출력.

한계(정직 고지):
- 수익률은 buy-and-hold 근사 — 실제 출구 규칙(트레일링 등)과 다르다. 여기서 재는 건
  발굴 '원재료'이지 전략 손익이 아니다. 왕복비용 ~0.33% 를 넘어야 실전 의미.
- 순매수 축의 풀 순서는 '시장 내 순위 인터리브' 병합 결과(market/rank.py 참조).
  레코드에 market/rank/metric 이 남아 있으므로 다른 병합 규칙 재구성도 가능하다.
- 휴장일 배제: 휴장이어도 수집 잡이 돌면 프로즌 스냅샷(전 종목 0.00%)이 쌓임 —
  전 종목 수익률이 정확히 0 인 날은 표본에서 자동 배제.
- 스냅샷 결손은 종목-일 단위로 스킵하고 건수를 표기 — 조용히 빠지지 않게.

실행: uv run python scripts/diagnostics/rank_signal_ab_study.py
      uv run python scripts/diagnostics/rank_signal_ab_study.py --selftest  # 데이터 없이 로직 검증
"""

from __future__ import annotations

import json
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

from auto_trading.reports.snapshot_reader import TopcapSnapshots, parse_topcap_for_date

ALT_RANK_PATH = Path("data/discovery/alt-rank.jsonl")
SNAPSHOT_ROOT = Path("data/snapshots")
ENTRY_MIN = 9 * 60 + 10  # 09:10 — 실제 봇 첫 진입 창
EXIT_MIN = 15 * 60 + 15  # 15:15 — EOD 청산 시각
TOL_MIN = 12  # 스냅샷 10분 캐던스 — 목표 시각에서 이 이상 멀면 결손 처리
TOP_N = 3  # 실제 픽 크기
ROUND_TRIP_COST_PCT = 0.33  # 왕복비용 근사 (reports/daily COST_* 합)
MIN_DAYS_FOR_VERDICT = 15

BASELINE = "volume"  # 현행 발굴 신호
PRIMARY_AXIS = "foreign_netbuy"  # 사전 등록된 1차 가설
AXIS_LABELS = {
    "volume": "거래량(현행)",
    "value": "거래대금",
    "foreign_netbuy": "외인순매수",
    "institution_netbuy": "기관순매수",
}


def _label(axis: str) -> str:
    return AXIS_LABELS.get(axis, axis)


@dataclass(frozen=True)
class DayRecord:
    date: str
    pools: dict[str, list[str]]  # 축 이름 → 풀 종목 (BASELINE 포함)
    picks: dict[str, list[str]]  # 축 이름 → 상위 TOP_N 픽
    blocked: bool  # volume_chosen 이 비었던 날(게이트 전면 차단)


def _pool_symbols(rows: object) -> list[str]:
    if not isinstance(rows, list):
        return []
    out: list[str] = []
    for row in rows:
        if isinstance(row, dict) and row.get("symbol"):
            out.append(str(row["symbol"]))
    return out


def parse_record(rec: dict) -> DayRecord | None:
    """alt-rank 한 줄 → DayRecord. v1(value_pool 만) · v2(alt_pools) 모두 지원."""
    volume_pool = [str(s) for s in rec.get("volume_pool") or []]
    if not volume_pool:
        return None
    chosen = [str(s) for s in rec.get("volume_chosen") or []]
    pools: dict[str, list[str]] = {BASELINE: volume_pool}

    alt = rec.get("alt_pools")
    if isinstance(alt, dict):
        for name, rows in alt.items():
            syms = _pool_symbols(rows)
            if syms:
                pools[str(name)] = syms
    elif rec.get("value_pool"):  # v1 레코드 — 거래대금 축만 있음
        syms = _pool_symbols(rec.get("value_pool"))
        if syms:
            pools["value"] = syms

    if len(pools) < 2:  # 대안 축이 하나도 없으면 비교 불가
        return None
    picks = {name: pool[:TOP_N] for name, pool in pools.items()}
    # 거래량 축의 '픽' 은 실제로 산 종목 — 차단일엔 풀 상위로 대체(다른 축과 동일 조건).
    picks[BASELINE] = (chosen or volume_pool)[:TOP_N]
    return DayRecord(date=str(rec.get("date")), pools=pools, picks=picks, blocked=not chosen)


def load_day_records(path: Path = ALT_RANK_PATH) -> list[DayRecord]:
    if not path.exists():
        return []
    rows: list[DayRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        parsed = parse_record(rec)
        if parsed is not None:
            rows.append(parsed)
    return rows


def price_at(snaps: TopcapSnapshots, symbol: str, minute: int) -> float | None:
    """목표 시각에 가장 가까운 스냅샷 가격. TOL_MIN 초과로 멀면 None(결손)."""
    pts = snaps.series.get(symbol)
    if not pts:
        return None
    nearest = min(pts, key=lambda p: abs(p.minute - minute))
    if abs(nearest.minute - minute) > TOL_MIN:
        return None
    return nearest.price


def window_return_pct(snaps: TopcapSnapshots, symbol: str) -> float | None:
    entry = price_at(snaps, symbol, ENTRY_MIN)
    exit_ = price_at(snaps, symbol, EXIT_MIN)
    if entry is None or exit_ is None or entry <= 0:
        return None
    return (exit_ / entry - 1.0) * 100.0


def kospi_return_pct(date: str, root: Path = SNAPSHOT_ROOT) -> float | None:
    """같은 창(09:10→15:15)의 KOSPI 지수 변화율 — 시장 통제군."""
    path = root / f"indices-{date}.jsonl"
    if not path.exists():
        return None
    pts: list[tuple[int, float]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("market") != "KOSPI":
            continue
        ts = str(rec.get("ts") or "")
        value = rec.get("value")
        if len(ts) < 16 or not isinstance(value, int | float):
            continue
        pts.append((int(ts[11:13]) * 60 + int(ts[14:16]), float(value)))
    if not pts:
        return None

    def nearest(minute: int) -> float | None:
        m, v = min(pts, key=lambda p: abs(p[0] - minute))
        return v if abs(m - minute) <= TOL_MIN else None

    entry, exit_ = nearest(ENTRY_MIN), nearest(EXIT_MIN)
    if entry is None or exit_ is None or entry <= 0:
        return None
    return (exit_ / entry - 1.0) * 100.0


def describe(returns: list[float]) -> str:
    if not returns:
        return "표본 0"
    wins = sum(1 for r in returns if r > 0)
    return (
        f"n={len(returns):3d} 평균 {statistics.fmean(returns):+6.2f}% "
        f"중앙 {statistics.median(returns):+6.2f}% 승률 {wins}/{len(returns)}"
    )


def paired_t(diffs: list[float]) -> float | None:
    """페어드 t 통계량. 표본 3 미만이거나 분산 0 이면 None."""
    if len(diffs) < 3:
        return None
    sd = statistics.stdev(diffs)
    if sd <= 0:
        return None
    return statistics.fmean(diffs) / (sd / len(diffs) ** 0.5)


@dataclass
class AxisStats:
    pool_returns: list[float] = field(default_factory=list)
    pick_returns: list[float] = field(default_factory=list)
    excess: list[float] = field(default_factory=list)
    day_pool_mean: dict[str, float] = field(default_factory=dict)
    overlap_with_baseline: list[float] = field(default_factory=list)


def _verdict_lines(stats: dict[str, AxisStats]) -> list[str]:
    """사전 등록된 판정 규칙 — 1차 가설(외인 순매수)에만 적용.

    표본 문턱은 **두 축이 함께 있는 날 수**로 잰다. 전체 평가일로 재면 1차 가설
    수집 이전의 과거 레코드(거래대금만 있던 v1 일자)까지 세어 조기 판정이 난다.
    """
    out: list[str] = []
    primary, base = stats.get(PRIMARY_AXIS), stats.get(BASELINE)
    if primary is None or base is None:
        return [
            f"⏳ 1차 가설 축({_label(PRIMARY_AXIS)}) 데이터 없음 — 판정 불가.",
            "   수집은 다음 발굴부터 시작됩니다 (v2 레코드가 쌓이면 이 줄이 판정으로 바뀜).",
        ]

    common = sorted(set(primary.day_pool_mean) & set(base.day_pool_mean))
    diffs = [primary.day_pool_mean[d] - base.day_pool_mean[d] for d in common]
    t = paired_t(diffs)
    mean_diff = statistics.fmean(diffs) if diffs else 0.0
    pos = sum(1 for d in diffs if d > 0)
    pick_gap = (
        statistics.fmean(primary.pick_returns) - statistics.fmean(base.pick_returns)
        if primary.pick_returns and base.pick_returns
        else 0.0
    )
    overlap = (
        statistics.fmean(primary.overlap_with_baseline) if primary.overlap_with_baseline else 0
    )

    cond1 = t is not None and abs(t) >= 2.0 and mean_diff > 0
    cond2 = pick_gap >= ROUND_TRIP_COST_PCT
    cond3 = overlap <= 0.7

    out.append(
        f"판정 대상(1차 가설): {_label(PRIMARY_AXIS)} vs {_label(BASELINE)} · 공통 {len(diffs)}일"
    )
    if len(diffs) < MIN_DAYS_FOR_VERDICT:
        out.append(
            f"⏳ 판정 보류 — 공통 표본 {len(diffs)}/{MIN_DAYS_FOR_VERDICT}일."
            " 아래 조건별 수치는 방향 참고용이며 확정 판정이 아닙니다."
        )
    t_str = f"t≈{t:.2f}" if t is not None else "t 계산 불가(표본·분산 부족)"
    out.append(
        f"  ① 풀 평균 차 유의(|t|≥2 & 양수): {'✅' if cond1 else '❌'} "
        f"평균 {mean_diff:+.2f}%p · 우위 {pos}/{len(diffs)}일 · {t_str}"
    )
    out.append(
        f"  ② top{TOP_N} 픽 차이가 왕복비용({ROUND_TRIP_COST_PCT}%) 이상: "
        f"{'✅' if cond2 else '❌'} {pick_gap:+.2f}%p"
    )
    out.append(
        f"  ③ 풀 겹침이 작아 검정력 있음(≤70%): {'✅' if cond3 else '❌'} 겹침 {overlap:.0%}"
    )
    if len(diffs) < MIN_DAYS_FOR_VERDICT:
        return out  # 표본 미달이면 채택/기각 문구를 아예 내지 않는다(조기 결론 방지).
    if cond1 and cond2 and cond3:
        out.append("→ **채택 후보** — 발굴 소스 교체를 검토할 근거가 처음으로 생김.")
        out.append("   단, 교체 전 반사실(출구 규칙 반영)로 손익까지 확인할 것.")
    else:
        out.append(
            "→ **기각** — 현행 거래량 순위 유지. 미충족 조건이 하나라도 있으면 교체하지 않는다."
        )
    return out


def analyze(records: list[DayRecord], *, snapshot_root: Path = SNAPSHOT_ROOT) -> dict:
    stats: dict[str, AxisStats] = {}
    skipped_symbol_days = 0
    blocked_days = 0
    holiday_days: list[str] = []
    evaluated_days = 0
    lines: list[str] = []

    for rec in records:
        snaps = parse_topcap_for_date(rec.date, root=snapshot_root)
        kospi = kospi_return_pct(rec.date, root=snapshot_root)

        universe: set[str] = set()
        for pool in rec.pools.values():
            universe |= set(pool)
        for pick in rec.picks.values():
            universe |= set(pick)
        ret = {sym: window_return_pct(snaps, sym) for sym in universe}

        # 휴장일 가드 — 휴장이어도 수집 잡은 돌기 때문에 전일 종가로 고정된 스냅샷이
        # 하루치 쌓여 전 종목 0.00% 로 조인된다. 평균 희석·표본 부풀림 방지로 통째 배제.
        nonnull = [r for r in ret.values() if r is not None]
        if nonnull and all(abs(r) < 1e-12 for r in nonnull):
            holiday_days.append(rec.date)
            lines.append(f"{rec.date} [휴장 배제] 전 종목 0.00% — 프로즌 스냅샷, 표본 제외")
            continue
        evaluated_days += 1
        blocked_days += rec.blocked

        base_pool = set(rec.pools.get(BASELINE, []))
        kospi_str = f"{kospi:+.2f}%" if kospi is not None else "결손"
        flag = " [차단일]" if rec.blocked else ""
        lines.append(f"{rec.date}{flag} · KOSPI {kospi_str}")

        for axis, pool in rec.pools.items():
            st = stats.setdefault(axis, AxisStats())
            day_returns: list[float] = []
            for sym in pool:
                r = ret.get(sym)
                if r is None:
                    skipped_symbol_days += 1
                    continue
                day_returns.append(r)
                st.pool_returns.append(r)
                if kospi is not None:
                    st.excess.append(r - kospi)
            for sym in rec.picks.get(axis, []):
                r = ret.get(sym)
                if r is not None:
                    st.pick_returns.append(r)
            if day_returns:
                st.day_pool_mean[rec.date] = statistics.fmean(day_returns)
            if axis != BASELINE and pool:
                st.overlap_with_baseline.append(len(set(pool) & base_pool) / len(pool))
            ov = "" if axis == BASELINE else f" 겹침 {len(set(pool) & base_pool)}/{len(pool)}"
            lines.append(f"    {_label(axis):<12}{describe(day_returns)}{ov}")

    return {
        "stats": stats,
        "lines": lines,
        "evaluated_days": evaluated_days,
        "blocked_days": blocked_days,
        "holiday_days": holiday_days,
        "skipped_symbol_days": skipped_symbol_days,
    }


def main() -> None:
    records = load_day_records()
    if not records:
        print(f"데이터 없음: {ALT_RANK_PATH} 에 비교 가능한 레코드가 없습니다.")
        print("paper-day 발굴이 돌면 하루 1줄씩 쌓입니다. 며칠 후 재실행하세요.")
        return

    res = analyze(records)
    stats: dict[str, AxisStats] = res["stats"]
    print(f"=== 발굴 신호 A/B (창 09:10→15:15, 왕복비용 {ROUND_TRIP_COST_PCT}%) ===")
    print(f"1차 가설: {_label(PRIMARY_AXIS)} · 나머지 축은 참고용(다중비교 주의)\n")
    for line in res["lines"]:
        print(line)

    print("\n--- 종합 (종목-일 단위) ---")
    for axis, st in stats.items():
        excess = f" · KOSPI 대비 {statistics.fmean(st.excess):+.2f}%p" if st.excess else ""
        print(f"{_label(axis):<12} 풀 전체: {describe(st.pool_returns)}{excess}")
    print()
    for axis, st in stats.items():
        print(f"{_label(axis):<12} top{TOP_N}(픽 시뮬): {describe(st.pick_returns)}")

    base = stats.get(BASELINE)
    if base is not None:
        print("\n--- 페어드 (일별 풀 평균 차, 각 축 빼기 거래량) ---")
        for axis, st in stats.items():
            if axis == BASELINE:
                continue
            common = sorted(set(st.day_pool_mean) & set(base.day_pool_mean))
            diffs = [st.day_pool_mean[d] - base.day_pool_mean[d] for d in common]
            if not diffs:
                continue
            t = paired_t(diffs)
            t_str = f" · t≈{t:.2f}" if t is not None else ""
            pos = sum(1 for d in diffs if d > 0)
            print(
                f"{_label(axis):<12} 평균 {statistics.fmean(diffs):+.2f}%p"
                f" · 우위 {pos}/{len(diffs)}일{t_str}"
            )

    holiday_str = f", 휴장 배제 {len(res['holiday_days'])}일" if res["holiday_days"] else ""
    print(
        f"\n표본: {res['evaluated_days']}일 (차단일 {res['blocked_days']}일 포함{holiday_str})"
        f" · 스냅샷 결손 스킵 {res['skipped_symbol_days']}종목-일"
    )
    print()
    for line in _verdict_lines(stats):
        print(line)


def selftest() -> None:
    """데이터 없이 파싱·집계 로직 자가 검증 (shadow_gate_study 와 같은 정신)."""
    v2 = {
        "v": 2,
        "date": "2026-08-10",
        "volume_chosen": ["A", "B"],
        "volume_pool": ["A", "B", "C"],
        "alt_pools": {
            "foreign_netbuy": [{"symbol": "X"}, {"symbol": "A"}],
            "institution_netbuy": [{"symbol": "Y"}],
        },
    }
    rec = parse_record(v2)
    assert rec is not None
    assert rec.pools[BASELINE] == ["A", "B", "C"]
    assert rec.pools["foreign_netbuy"] == ["X", "A"]
    assert rec.picks[BASELINE] == ["A", "B"]  # 실제 픽 우선
    assert not rec.blocked

    blocked = parse_record({**v2, "volume_chosen": []})
    assert blocked is not None and blocked.blocked
    assert blocked.picks[BASELINE] == ["A", "B", "C"][:TOP_N]  # 차단일은 풀 상위로 대체

    v1 = {"v": 1, "date": "2026-07-06", "volume_pool": ["A"], "value_pool": [{"symbol": "Z"}]}
    rec1 = parse_record(v1)
    assert rec1 is not None and rec1.pools["value"] == ["Z"]

    assert parse_record({"volume_pool": ["A"]}) is None  # 대안 축 없으면 비교 불가
    assert paired_t([1.0, 1.0, 1.0]) is None  # 분산 0
    assert paired_t([1.0, 2.0]) is None  # 표본 부족
    t = paired_t([1.0, 2.0, 3.0])
    assert t is not None and t > 0

    st = {
        BASELINE: AxisStats(pick_returns=[0.0], day_pool_mean={"d1": 0.0}),
        PRIMARY_AXIS: AxisStats(pick_returns=[1.0], day_pool_mean={"d1": 1.0}),
    }
    lines = _verdict_lines(st)
    # 공통 1일 = 표본 미달 → 채택/기각 문구가 나오면 안 된다(조기 결론 방지).
    assert any("판정 보류" in line for line in lines)
    assert not any("채택" in line or "기각" in line for line in lines)
    assert _verdict_lines({BASELINE: AxisStats()})[0].startswith("⏳")
    print("✅ selftest 통과 — 파싱(v1/v2)·차단일 대체·페어드 t·판정 라인 정상")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()
