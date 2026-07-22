"""발굴 신호 A/B — 거래량 순위 vs 거래대금 순위 원재료 수익률 비교 (read-only).

무엇/왜: PR #164 가 매 거래일 발굴 시점에 두 순위(현행 거래량 상위 / 대안 거래대금
상위)를 `data/discovery/alt-rank.jsonl` 한 줄로 축적 중. 이 스크립트는 그 기록을
자체 스냅샷(`data/snapshots/topcap+discovered-*.jsonl`, 10분 캐던스)과 조인해
"어느 순위의 원재료(봇 진입 창 09:10→15:15 수익률)가 나은가"를 판정한다 —
selection_alpha_study 의 방법을 두 풀 비교로 확장한 것.

측정:
1. 풀 전체 — 두 풀 각각의 종목-일 수익률 분포(평균·중앙값·승률) + KOSPI 지수 대비
   초과수익(같은 창의 indices-*.jsonl 지수 변화로 통제).
2. 픽 시뮬 — 실제 봇 위치인 상위 3개(top_n=3)만 비교. 거래량 쪽은 volume_chosen
   (차단일엔 volume_pool 상위 3), 거래대금 쪽은 value_pool 상위 3.
3. 페어드 — 같은 날 두 풀 평균의 차이(거래량 - 거래대금)로 일별 부호 일관성 확인.

한계(정직 고지):
- 판정은 거래일 15일 이상 모인 뒤(MIN_DAYS_FOR_VERDICT). 그 전엔 "판정 보류" 출력.
- 수익률은 buy-and-hold 근사 — 실제 출구 규칙(트레일링 등)과 다르다. 여기서 재는 건
  발굴 원재료이지 전략 손익이 아니다. 왕복비용 ~0.33% 를 넘어야 실전 의미.
- 스냅샷 커버리지: discovered-*.jsonl 은 2026-07-06 부터 — 그 전 일자는 조인 결손이
  많아 스킵 건수로 표기. 두 풀의 겹침이 크면(같은 종목) A/B 검정력이 낮다 — 겹침 표기.
- value_pool 순서는 키움 ka10030 sort_tp=2 기준(병합 정렬은 volumexprice 근사).
- 휴장일 배제: 휴장이어도 수집 잡이 돌면 프로즌 스냅샷(전 종목 0.00%)이 쌓임 —
  전 종목 수익률이 정확히 0 인 날은 표본에서 자동 배제 (snapshot_selection_study 동일).

실행: uv run python scripts/diagnostics/alt_rank_ab_study.py
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
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


@dataclass(frozen=True)
class DayRecord:
    date: str
    volume_picks: list[str]  # 실제 픽(차단일엔 풀 상위 TOP_N 로 대체)
    volume_pool: list[str]
    value_pool: list[str]
    blocked: bool  # volume_chosen 이 비었던 날(게이트 전면 차단)


def load_day_records() -> list[DayRecord]:
    if not ALT_RANK_PATH.exists():
        return []
    rows: list[DayRecord] = []
    for line in ALT_RANK_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        volume_pool = [str(s) for s in rec.get("volume_pool") or []]
        chosen = [str(s) for s in rec.get("volume_chosen") or []]
        value_pool = [str(e.get("symbol")) for e in rec.get("value_pool") or [] if e.get("symbol")]
        if not volume_pool or not value_pool:
            continue
        rows.append(
            DayRecord(
                date=str(rec.get("date")),
                volume_picks=chosen or volume_pool[:TOP_N],
                volume_pool=volume_pool,
                value_pool=value_pool,
                blocked=not chosen,
            )
        )
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


def kospi_return_pct(date: str) -> float | None:
    """같은 창(09:10→15:15)의 KOSPI 지수 변화율 — 시장 통제군."""
    path = SNAPSHOT_ROOT / f"indices-{date}.jsonl"
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
        minute = int(ts[11:13]) * 60 + int(ts[14:16])
        pts.append((minute, float(value)))
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
    med = statistics.median(returns)
    return (
        f"n={len(returns):3d} 평균 {statistics.fmean(returns):+6.2f}% "
        f"중앙 {med:+6.2f}% 승률 {wins}/{len(returns)}"
    )


def main() -> None:
    records = load_day_records()
    if not records:
        print(f"데이터 없음: {ALT_RANK_PATH} 가 비어 있습니다.")
        print("paper-day 발굴이 돌면 하루 1줄씩 쌓입니다 (PR #164). 며칠 후 재실행하세요.")
        return

    vol_all: list[float] = []
    val_all: list[float] = []
    vol_top: list[float] = []
    val_top: list[float] = []
    vol_excess: list[float] = []
    val_excess: list[float] = []
    day_diffs: list[float] = []  # 일별 (거래량 풀 평균 - 거래대금 풀 평균)
    skipped_symbol_days = 0
    blocked_days = 0
    holiday_days: list[str] = []
    evaluated_days = 0

    print(f"=== 발굴 A/B: 거래량 vs 거래대금 (창 09:10→15:15, 왕복비용 {ROUND_TRIP_COST_PCT}%) ===")
    for rec in records:
        snaps = parse_topcap_for_date(rec.date, root=SNAPSHOT_ROOT)
        kospi = kospi_return_pct(rec.date)
        overlap = len(set(rec.volume_pool) & set(rec.value_pool))

        universe = set(rec.volume_pool) | set(rec.value_pool) | set(rec.volume_picks[:TOP_N])
        ret = {sym: window_return_pct(snaps, sym) for sym in universe}
        # 휴장일 가드 — 휴장이어도 수집 잡은 돌기 때문에(예: 2026-07-17) 전일 종가로
        # 고정된 스냅샷이 하루치 쌓여 전 종목 0.00% 로 조인된다. 그대로 두면 평균이
        # 0 쪽으로 희석되고 표본 일수도 부풀므로 통째로 배제한다.
        nonnull = [r for r in ret.values() if r is not None]
        if nonnull and all(abs(r) < 1e-12 for r in nonnull):
            holiday_days.append(rec.date)
            print(f"{rec.date} [휴장 배제] 전 종목 0.00% — 프로즌 스냅샷, 표본 제외")
            continue
        evaluated_days += 1
        blocked_days += rec.blocked

        day_vol: list[float] = []
        day_val: list[float] = []
        for sym in rec.volume_pool:
            r = ret.get(sym)
            if r is None:
                skipped_symbol_days += 1
                continue
            day_vol.append(r)
            vol_all.append(r)
            if kospi is not None:
                vol_excess.append(r - kospi)
        for sym in rec.value_pool:
            r = ret.get(sym)
            if r is None:
                skipped_symbol_days += 1
                continue
            day_val.append(r)
            val_all.append(r)
            if kospi is not None:
                val_excess.append(r - kospi)
        for sym in rec.volume_picks[:TOP_N]:
            r = ret.get(sym)
            if r is not None:
                vol_top.append(r)
        for sym in rec.value_pool[:TOP_N]:
            r = ret.get(sym)
            if r is not None:
                val_top.append(r)

        if day_vol and day_val:
            day_diffs.append(statistics.fmean(day_vol) - statistics.fmean(day_val))
        kospi_str = f"{kospi:+.2f}%" if kospi is not None else "결손"
        flag = " [차단일]" if rec.blocked else ""
        print(
            f"{rec.date}{flag} 겹침 {overlap}/{len(rec.volume_pool)} · KOSPI {kospi_str}\n"
            f"    거래량   {describe(day_vol)}\n"
            f"    거래대금 {describe(day_val)}"
        )

    print("\n--- 종합 (종목-일 단위) ---")
    print(f"거래량   풀 전체: {describe(vol_all)}")
    print(f"거래대금 풀 전체: {describe(val_all)}")
    if vol_excess and val_excess:
        print(
            f"KOSPI 대비 초과: 거래량 {statistics.fmean(vol_excess):+.2f}%p"
            f" / 거래대금 {statistics.fmean(val_excess):+.2f}%p"
        )
    print(f"거래량   top{TOP_N}(픽 시뮬): {describe(vol_top)}")
    print(f"거래대금 top{TOP_N}(픽 시뮬): {describe(val_top)}")

    if day_diffs:
        pos = sum(1 for d in day_diffs if d > 0)
        line = (
            f"\n페어드(일별 풀 평균 차, 거래량-거래대금): 평균 {statistics.fmean(day_diffs):+.2f}%p"
            f" · 거래량 우위 {pos}/{len(day_diffs)}일"
        )
        if len(day_diffs) >= 3:
            sd = statistics.stdev(day_diffs)
            if sd > 0:
                t = statistics.fmean(day_diffs) / (sd / len(day_diffs) ** 0.5)
                line += f" · t≈{t:.2f}"
        print(line)

    holiday_str = f", 휴장 배제 {len(holiday_days)}일" if holiday_days else ""
    print(
        f"\n표본: {evaluated_days}일 (차단일 {blocked_days}일 포함{holiday_str})"
        f" · 스냅샷 결손 스킵 {skipped_symbol_days}종목-일"
    )
    if evaluated_days < MIN_DAYS_FOR_VERDICT:
        print(
            f"⏳ 판정 보류 — 거래일 {MIN_DAYS_FOR_VERDICT}일 이상 모이면 판정"
            f" (현재 {evaluated_days}일). 위 수치는 방향 참고용."
        )
    else:
        print(
            "판정 기준: ① 페어드 차이의 부호가 일관(2/3 이상 같은 방향)"
            f" ② top{TOP_N} 평균 차이가 왕복비용 {ROUND_TRIP_COST_PCT}% 이상"
            " ③ 겹침이 커서(>7/10) 차이가 안 나면 '신호 교체 무의미' 결론."
        )


if __name__ == "__main__":
    main()
