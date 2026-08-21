"""프레임워크 판정 — 봇 로그만으로 4대 주장 검증 (read-only, API 호출 없음).

무엇/왜: 2026-08-15~17 의 전략 전면 재검토에서 나온 핵심 주장 4개를 **봇이 라이브로
기록한 데이터만으로**(fdr·mock 순위 배제) 재현하는 판정 기록. topcap 스냅샷은 매일
08:50 에 그날의 시총 상위 200 을 박제하므로 생존편향이 없다. 이 스크립트가 이 저장소
전략 탐색 종결(memory: project_structure_search_closed)의 1차 근거다.

판정 결과 (최초 확정 2026-08-17 · 표본은 수집 잡이 매일 늘리므로 재실행 수치는
조금씩 달라진다 — 지금까지의 갱신은 전부 결론을 강화하는 방향이었다):
  [1] 실측 왕복비용 = 0.316% (17 진입일, 매수대금 3.18억 vs 비용 100.5만원)
  [2] topcap 200 장중(개장->마감) 음(-): 56일 -0.226 -> 61일 -0.257%/일 (일별 CI 0 포함)
  [3] 오버나이트(전일종가->개장) 양(+): +0.29%/일 안팎 (일별 CI 0 포함 — 방향 참고)
  [4] 봇 발굴 풀 - 벤치마크: 26일 -1.78%p·CI [-2.22,-0.95] -> 30일 **-1.90%p ·
      열세 21/30일 · CI [-2.50, -1.11]** <- 0 확실히 배제, 표본이 늘수록 더 단단.
      **손실의 뿌리는 '아침 거래량 상위' 라는 풀 정의 자체.**
  종합: 장중 일회전 손익분기에 선정 초과수익 >= 0.54~0.57%/거래(연 ~135-143%) 필요.
  관측된 어떤 신호도(단일·결합·미분/적분 계열) 0.15%p 를 넘지 못했다.

주의: 봇의 발굴 순위(mock ka10030)는 실제 시장 거래량이 아님이 확정돼 있다
(memory: project_mock_rank_not_real). [4] 는 "봇이 실제로 산 풀" 의 성과이므로
그 사실과 무관하게 성립한다 — 체결가는 실제와 1틱 내외로 일치.

실행: uv run python scripts/diagnostics/framework_verdict_study.py
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

from indicator_harness import moving_block_ci

from auto_trading.reports.snapshot_reader import parse_topcap_for_date

SNAP = Path("data/snapshots")
REPORTS = Path("data/reports")
ALT_RANK = Path("data/discovery/alt-rank.jsonl")

OPEN_MAX = 9 * 60 + 15  # 첫 스냅샷이 이 시각 이전이어야 '개장 관측' 으로 인정
CLOSE_MIN = 15 * 60  # 마지막 스냅샷이 이 시각 이후여야 '마감 관측' 으로 인정
ENTRY_MIN, EXIT_MIN, TOL = 9 * 60 + 10, 15 * 60 + 15, 7


def _ci_str(ci: tuple[float, float] | None) -> str:
    return f"[{ci[0]:+.3f}, {ci[1]:+.3f}]" if ci else "CI 불가"


def verify_costs() -> float:
    """[1] 리포트에 기록된 실제 비용 — 왕복비용 % 반환."""
    print("=" * 66)
    print("[1] 왕복비용 — 리포트 실기록")
    print("=" * 66)
    rows = []
    for rp in sorted(REPORTS.glob("daily-*.json")):
        d = json.loads(rp.read_text())
        buys = d.get("buys") or []
        if not buys:
            continue
        notional = sum(b["price"] * b["quantity"] for b in buys if b.get("price"))
        cost = (
            (d.get("total_fees") or 0)
            + (d.get("total_taxes") or 0)
            + (d.get("total_slippage") or 0)
        )
        if notional > 0:
            rows.append((notional, cost, cost / notional * 100))
    tot_n, tot_c = sum(r[0] for r in rows), sum(r[1] for r in rows)
    pct = tot_c / tot_n * 100
    print(f"진입일 {len(rows)}일 · 총 매수대금 {tot_n:,.0f}원 · 총 비용 {tot_c:,.0f}원")
    print(
        f"-> 실측 왕복비용 = {pct:.3f}% (일별 중앙 {statistics.median([r[2] for r in rows]):.3f}%)"
    )
    print("  (수수료·거래세는 실제 요율, 슬리피지는 리포트의 추정 상수)")
    return pct


def verify_windows() -> float:
    """[2][3] 장중 vs 오버나이트 — topcap 스냅샷. 장중 드리프트(%/일) 반환."""
    print()
    print("=" * 66)
    print("[2][3] 장중 vs 오버나이트 — topcap 스냅샷 (시총 상위 200, 라이브 기록)")
    print("=" * 66)
    day_over, day_intra = {}, {}
    for p in sorted(SNAP.glob("topcap-*.jsonl")):
        ds = p.stem.replace("topcap-", "")
        snaps = parse_topcap_for_date(ds)
        ov, it, tt = [], [], []
        for _sym, pts in snaps.series.items():
            if not pts:
                continue
            first, last = pts[0], pts[-1]
            if first.minute > OPEN_MAX or last.minute < CLOSE_MIN or first.price <= 0:
                continue
            ov.append(first.change_pct)  # 전일 종가 -> 개장 직후 (오버나이트 갭)
            it.append((last.price / first.price - 1) * 100)  # 개장 직후 -> 마감 (장중)
            tt.append(last.change_pct)
        if len(ov) < 50 or all(abs(x) < 1e-12 for x in tt):  # 커버 부족/휴장 제외
            continue
        day_over[ds] = statistics.fmean(ov)
        day_intra[ds] = statistics.fmean(it)
    mo = statistics.fmean(list(day_over.values()))
    mi = statistics.fmean(list(day_intra.values()))
    print(f"평가 {len(day_intra)}일")
    print(f"{'구간':<28}{'일평균':>9}{'연환산':>9}{'일별 95% CI':>22}")
    print(
        f"{'오버나이트 (전일종가->개장)':<27}{mo:>+8.3f}%{mo * 250:>+8.1f}%"
        f"{_ci_str(moving_block_ci(list(day_over.values()), block=5)):>22}"
    )
    print(
        f"{'장중 (개장->마감)':<29}{mi:>+8.3f}%{mi * 250:>+8.1f}%"
        f"{_ci_str(moving_block_ci(list(day_intra.values()), block=5)):>22}"
    )
    return mi


def _price_at(snaps, sym: str, minute: int) -> float | None:
    pts = snaps.series.get(sym)
    if not pts:
        return None
    nearest = min(pts, key=lambda p: abs(p.minute - minute))
    return nearest.price if abs(nearest.minute - minute) <= TOL else None


def _bot_pools() -> dict[str, list[str]]:
    pools: dict[str, list[str]] = {}
    for rp in sorted(REPORTS.glob("daily-*.json")):
        d = json.loads(rp.read_text())
        if d.get("rank_candidates"):
            pools[d["date"]] = [str(s) for s in d["rank_candidates"]]
    if not ALT_RANK.exists():
        return pools
    for line in ALT_RANK.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(rec.get("round") or "0902") != "0902":
            continue
        rws = rec.get("volume_pool_rows")
        syms = (
            [str(r["symbol"]) for r in rws if isinstance(r, dict) and r.get("symbol")]
            if isinstance(rws, list)
            else [str(s) for s in rec.get("volume_pool") or []]
        )
        if syms:
            pools[str(rec.get("date"))] = syms
    return pools


def verify_pool_deficit() -> None:
    """[4] 봇 발굴 풀 vs topcap 벤치마크 — 같은 날·같은 창 09:10->15:15."""
    print()
    print("=" * 66)
    print("[4] 봇 발굴 풀 vs topcap 벤치마크 — 같은 날·같은 창 09:10->15:15")
    print("=" * 66)
    diffs, pool_ms, bench_ms = [], [], []
    for ds, pool in sorted(_bot_pools().items()):
        snaps = parse_topcap_for_date(ds)
        pool_rets = [
            (x / e - 1) * 100
            for sym in pool
            if (e := _price_at(snaps, sym, ENTRY_MIN)) and (x := _price_at(snaps, sym, EXIT_MIN))
        ]
        bench_rets = [
            (x / e - 1) * 100
            for sym in snaps.series
            if (e := _price_at(snaps, sym, ENTRY_MIN)) and (x := _price_at(snaps, sym, EXIT_MIN))
        ]
        if len(pool_rets) < 4 or len(bench_rets) < 50:
            continue
        if all(abs(r) < 1e-12 for r in bench_rets):
            continue
        p_m, b_m = statistics.fmean(pool_rets), statistics.fmean(bench_rets)
        pool_ms.append(p_m)
        bench_ms.append(b_m)
        diffs.append(p_m - b_m)
    gap = statistics.fmean(diffs)
    n_neg = sum(1 for x in diffs if x < 0)
    print(f"공통 평가일 {len(diffs)}일")
    print(
        f"봇 풀 평균 {statistics.fmean(pool_ms):+.2f}% · 벤치마크 평균 {statistics.fmean(bench_ms):+.2f}%"
    )
    print(
        f"풀 - 벤치마크 = {gap:+.2f}%p · 열세 {n_neg}/{len(diffs)}일 · "
        f"95% CI {_ci_str(moving_block_ci(diffs, block=5))}"
    )


def main() -> None:
    cost = verify_costs()
    drift = verify_windows()
    verify_pool_deficit()
    print()
    print("=" * 66)
    print("[종합] 장중 일회전 손익분기 문턱 (전부 로그 실측치)")
    print("=" * 66)
    print(f"  실측 왕복비용     : {cost:+.3f}%")
    print(f"  실측 장중 드리프트 : {drift:+.3f}%/일 (topcap 200)")
    print(
        f"  -> 선정이 벌어야 할 초과수익 >= {cost - drift:.3f}%/거래 (연 {(cost - drift) * 250:.0f}%)"
    )


if __name__ == "__main__":
    main()
