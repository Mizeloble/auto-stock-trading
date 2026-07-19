"""자체 10분 스냅샷 기반 선별 축 판정 — 2026-07-16 사용자 제안 선작성 (read-only).

무엇/왜: "당일 거래량 순위 대신, 쌓아둔 종목별 장중 히스토리로 고르면 낫지 않나"
  (2026-07-16). 일봉 히스토리 재정렬은 3년 반사실에서 개선 없음이었지만
  (selection_study 2026-06-07), 일봉으로는 못 보는 장중 축은 자체 스냅샷이
  쌓여야 검증 가능 — alt_rank_ab_study 와 같은 "선작성 후 표본 차면 판정" 패턴.

방법 (API 호출 없음, 전부 자체 축적분):
- 풀: data/discovery/alt-rank.jsonl 의 당일 발굴 풀(거래량 순위 10종목).
- 시세: data/snapshots/{topcap,discovered}-*.jsonl 병합 (10분 간격, 발굴 종목은
  2026-07-06 이후 전 종목 커버 — PR #165).
- 같은 풀에서 4가지 순위로 top3 를 뽑아 원재료 수익률(신호 스냅샷 → 당일 마지막
  스냅샷, 동일가중)을 비교:
    baseline  현행 = 풀 순서(개장 거래량 순위)
    open_flow 개장 10분 거래대금(09:15 이전 마지막 스냅샷의 price x volume) 상위
    prev_pm   전일 14:00→마감 수익률 상위 (쌓아둔 히스토리)
    gap       전일 마감→금일 시초 갭 상위 (쌓아둔 히스토리)
- 판정: 평가 가능일 15일 미만이면 "판정 보류" (진행 현황만 출력).

한계(정직 고지):
- 원재료 수익률 비교 — 실제 출구 스택(손절·트레일링) 미반영. 방향 참고용.
- 전일 히스토리 축은 전일 스냅샷에 없던 종목(신규 급등주)을 순위 불가로 제외
  → 커버리지를 함께 출력. 낮으면 그 축 판정 자체를 불신할 것.
- 신호는 09:15 이전 스냅샷만 사용(lookahead 방지), 진입가 = 신호 스냅샷 가격.

실행: uv run python scripts/diagnostics/snapshot_selection_study.py [--selftest]
"""

from __future__ import annotations

import json
import statistics
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path

MIN_DAYS = 15
PICK_K = 3
SIGNAL_CUTOFF = time(9, 15)  # 이 시각 이전 스냅샷만 신호에 사용 (진입 ~09:10 근사)
PREV_PM_START = time(14, 0)
SNAP_ROOT = Path("data/snapshots")
ALT_RANK = Path("data/discovery/alt-rank.jsonl")
OUT_PATH = Path("data/studies/snapshot-selection-study.json")
AXES = ["baseline", "open_flow", "prev_pm", "gap"]


@dataclass
class Snap:
    hhmm: time
    price: float
    volume: float


def load_pools(alt_rank_path: Path) -> dict[date, list[str]]:
    pools: dict[date, list[str]] = {}
    if not alt_rank_path.exists():
        return pools
    for line in alt_rank_path.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        pool = [str(s) for s in rec.get("volume_pool") or []]
        if rec.get("date") and pool:
            pools[date.fromisoformat(rec["date"])] = pool
    return pools


def load_snapshots(day: date, root: Path) -> dict[str, list[Snap]]:
    """topcap + discovered 병합 → 종목별 (시각, 가격, 누적거래량) 오름차순."""
    by_symbol: dict[str, list[Snap]] = {}
    for stem in ("topcap", "discovered"):
        path = root / f"{stem}-{day.isoformat()}.jsonl"
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts_raw, price = rec.get("ts"), rec.get("price")
            if not isinstance(ts_raw, str) or not isinstance(price, (int, float)) or price <= 0:
                continue
            hhmm = time.fromisoformat(ts_raw[11:19])
            by_symbol.setdefault(str(rec.get("symbol")), []).append(
                Snap(hhmm=hhmm, price=float(price), volume=float(rec.get("volume") or 0))
            )
    for series in by_symbol.values():
        series.sort(key=lambda s: s.hhmm)
    return by_symbol


def signal_snap(series: list[Snap]) -> Snap | None:
    """09:15 이전 마지막 스냅샷 — 신호·진입가 겸용 (lookahead 방지)."""
    before = [s for s in series if s.hhmm <= SIGNAL_CUTOFF]
    return before[-1] if before else None


def day_return_pct(series: list[Snap]) -> float | None:
    """신호 스냅샷 → 당일 마지막 스냅샷 수익률(%). 마감 전(진행 중) 날은 제외."""
    entry = signal_snap(series)
    if entry is None or series[-1].hhmm <= entry.hhmm:
        return None
    if series[-1].hhmm < time(15, 0):  # 장중 실행 시 오늘자 미완성 데이터 배제
        return None
    return (series[-1].price / entry.price - 1.0) * 100.0


def rank_axes(
    pool: list[str],
    today: dict[str, list[Snap]],
    prev: dict[str, list[Snap]],
) -> dict[str, list[str]]:
    """축별 순위(내림차순 픽 순서). 순위 산출 불가 종목은 그 축에서 제외."""
    open_flow: list[tuple[float, str]] = []
    prev_pm: list[tuple[float, str]] = []
    gap: list[tuple[float, str]] = []
    for sym in pool:
        sig = signal_snap(today.get(sym, []))
        if sig is not None:
            open_flow.append((sig.price * sig.volume, sym))
        prev_series = prev.get(sym) or []
        pm = [s for s in prev_series if s.hhmm >= PREV_PM_START]
        if len(pm) >= 2 and pm[0].price > 0:
            prev_pm.append((pm[-1].price / pm[0].price - 1.0, sym))
        if prev_series and sig is not None and prev_series[-1].price > 0:
            gap.append((sig.price / prev_series[-1].price - 1.0, sym))
    return {
        "baseline": list(pool),
        "open_flow": [s for _, s in sorted(open_flow, key=lambda x: -x[0])],
        "prev_pm": [s for _, s in sorted(prev_pm, key=lambda x: -x[0])],
        "gap": [s for _, s in sorted(gap, key=lambda x: -x[0])],
    }


def evaluate(pools: dict[date, list[str]], snap_root: Path) -> tuple[list[dict], dict[str, int]]:
    """일자별 축 성적. 반환: (일별 결과, 축별 순위산출 커버리지 카운트)."""
    days = sorted(pools)
    snap_days = sorted(
        d
        for d in days
        if (snap_root / f"topcap-{d.isoformat()}.jsonl").exists()
        or (snap_root / f"discovered-{d.isoformat()}.jsonl").exists()
    )
    results: list[dict] = []
    coverage = {axis: 0 for axis in AXES}
    prev: dict[str, list[Snap]] = {}  # 직전 "정상 거래일" 스냅샷 (휴장일은 건너뜀)
    for day in snap_days:
        today = load_snapshots(day, snap_root)
        pool = pools[day]
        returns = {s: day_return_pct(today.get(s, [])) for s in pool}
        # 휴장일 가드 — 휴장이어도 수집 잡은 돌기 때문에(예: 2026-07-17) 전일 종가로
        # 고정된 스냅샷이 하루치 쌓인다. 풀 전 종목 수익률이 정확히 0 이면 배제하고,
        # prev 도 갱신하지 않는다(다음 날의 "전일"은 마지막 정상 거래일이어야 함).
        nonnull = [v for v in returns.values() if v is not None]
        if nonnull and all(abs(v) < 1e-12 for v in nonnull):
            continue
        ranked = rank_axes(pool, today, prev)
        row: dict = {"day": day.isoformat(), "picks": {}, "mean_pct": {}}
        for axis in AXES:
            picks = [s for s in ranked[axis] if returns.get(s) is not None][:PICK_K]
            if len(picks) < PICK_K:
                row["mean_pct"][axis] = None
                continue
            coverage[axis] += 1
            row["picks"][axis] = picks
            vals = [returns[s] for s in picks]
            row["mean_pct"][axis] = sum(v for v in vals if v is not None) / len(vals)
        pool_vals = [v for v in returns.values() if v is not None]
        row["pool_mean_pct"] = sum(pool_vals) / len(pool_vals) if pool_vals else None
        results.append(row)
        prev = today
    return results, coverage


def print_report(results: list[dict], coverage: dict[str, int]) -> dict:
    evaluable = [r for r in results if r["mean_pct"].get("baseline") is not None]
    n = len(evaluable)
    print("=" * 76)
    print("자체 10분 스냅샷 선별 축 판정 — 현행(거래량 순위) vs 장중/히스토리 축")
    print(f"평가 가능일 {n}일 (기준 {MIN_DAYS}일) · 축별 순위산출 가능일: {coverage}")
    print("=" * 76)
    header = f"{'일자':<12}{'풀평균':>9}" + "".join(f"{a:>11}" for a in AXES)
    print(header)
    for r in results:
        cells = ""
        for a in AXES:
            v = r["mean_pct"].get(a)
            cells += f"{v:>+11.2f}" if v is not None else f"{'—':>11}"
        pool_v = r.get("pool_mean_pct")
        pool_cell = f"{pool_v:>+9.2f}" if pool_v is not None else f"{'—':>9}"
        print(f"{r['day']:<12}{pool_cell}{cells}")

    summary: dict = {"evaluable_days": n, "verdict": None, "axes": {}}
    if n == 0:
        summary["verdict"] = "데이터 없음"
        print("\n평가 가능일 0 — 스냅샷/풀 데이터 확인.")
        return summary

    print("-" * 76)
    base_by_day = {r["day"]: r["mean_pct"]["baseline"] for r in evaluable}
    for axis in AXES:
        vals = [
            (r["mean_pct"][axis], base_by_day[r["day"]])
            for r in evaluable
            if r["mean_pct"].get(axis) is not None
        ]
        if not vals:
            continue
        mean_axis = statistics.mean(v for v, _ in vals)
        diffs = [v - b for v, b in vals]
        mean_diff = statistics.mean(diffs)
        wins = sum(1 for d in diffs if d > 0)
        summary["axes"][axis] = {
            "days": len(vals),
            "mean_pct": mean_axis,
            "mean_diff_vs_baseline_pct": mean_diff,
            "win_days_vs_baseline": wins,
        }
        tag = (
            " (기준)"
            if axis == "baseline"
            else f"  Δ대비 {mean_diff:+.2f}%p · 우세 {wins}/{len(vals)}일"
        )
        print(f"{axis:<10} 평균 {mean_axis:+.2f}%{tag}")

    if n < MIN_DAYS:
        summary["verdict"] = f"판정 보류 — 평가 가능일 {n}/{MIN_DAYS}"
        print(f"\n⏳ {summary['verdict']} (표본이 차면 재실행)")
    else:
        best = max(
            (a for a in AXES if a != "baseline" and a in summary["axes"]),
            key=lambda a: summary["axes"][a]["mean_diff_vs_baseline_pct"],
            default=None,
        )
        summary["verdict"] = (
            f"판정 가능 — 최우수 대안 축: {best}" if best else "판정 가능 — 대안 축 없음"
        )
        print(f"\n✅ {summary['verdict']}")
        print("주의: 원재료 수익률 기준 — 채택 전 출구 스택 반영 반사실로 재확인할 것.")
    return summary


def selftest() -> None:
    """합성 데이터로 파이프라인 검증 — 축 순위·수익률 계산이 기대대로인지."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "snapshots").mkdir()
        days = [date(2026, 1, 5), date(2026, 1, 6)]
        # D1: A 는 오후 상승(prev_pm 1위 재료), B 는 하락. D2: A 는 +10%, B 는 -10%.
        rows_d1 = []
        for sym, pm_up in (("A", True), ("B", False), ("C", False)):
            for hhmm, base in (("09:10", 100), ("14:00", 100), ("15:30", 120 if pm_up else 90)):
                rows_d1.append(
                    {
                        "ts": f"2026-01-05T{hhmm}:00+09:00",
                        "symbol": sym,
                        "price": base,
                        "volume": 10,
                    }
                )
        rows_d2 = []
        for sym, end in (("A", 110), ("B", 90), ("C", 100)):
            for hhmm, px in (("09:10", 100), ("15:30", end)):
                rows_d2.append(
                    {
                        "ts": f"2026-01-06T{hhmm}:00+09:00",
                        "symbol": sym,
                        "price": px,
                        "volume": 1000 if sym == "A" else 10,
                    }
                )
        for day, rows in zip(days, (rows_d1, rows_d2), strict=True):
            (root / "snapshots" / f"topcap-{day.isoformat()}.jsonl").write_text(
                "\n".join(json.dumps(r) for r in rows)
            )
        alt = root / "alt-rank.jsonl"
        alt.write_text(
            "\n".join(
                json.dumps({"date": d.isoformat(), "volume_pool": ["B", "C", "A"]}) for d in days
            )
        )
        pools = load_pools(alt)
        results, _ = evaluate(pools, root / "snapshots")
        d2 = results[1]
        assert d2["picks"]["open_flow"][0] == "A", d2  # 거래대금 1위 = A
        assert d2["picks"]["prev_pm"][0] == "A", d2  # 전일 오후 상승 1위 = A
        assert abs(d2["mean_pct"]["baseline"] - 0.0) < 1e-9  # (B-10 + C0 + A+10)/3
        print("selftest OK — 축 순위·수익률 계산 정상")


def main() -> None:
    if "--selftest" in sys.argv:
        selftest()
        return
    pools = load_pools(ALT_RANK)
    results, coverage = evaluate(pools, SNAP_ROOT)
    summary = print_report(results, coverage)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(
            {"study": "snapshot-selection", "summary": summary, "days": results},
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"저장: {OUT_PATH}")


if __name__ == "__main__":
    main()
