"""발굴 신호 A/B (N축) — 거래량 순위 vs 대안 순위 원재료 수익률 비교 (read-only).

무엇/왜: 거래대금 A/B(`alt_rank_ab_study.py`, 2026-07-31 기각)의 후속. `alt-rank.jsonl`
v2 가 축을 여러 개 동시에 축적하기 시작해(외인/기관 순매수), 이 스크립트는 축이 몇 개든
같은 방법으로 한 번에 판정한다. 방법 자체는 selection_alpha_study → alt_rank_ab_study
계보를 그대로 잇는다(창 09:10→15:15 buy-and-hold, KOSPI 통제).

거래대금이 왜 기각됐는지 정확히(적대적 재검증 2026-08-09 — 착각 방지):
- 원인은 **풀 겹침이 아니다.** 실측 겹침은 22일 평균 37%(최대 60%)로 아래 조건 ③의
  70% 문턱을 한 번도 넘지 않았다. "겹쳐서 검정력이 없었다"는 서술은 틀렸다.
- 진짜 원인은 **풀 수준 우위가 top3 픽에서 뒤집힌 것**(풀은 거래대금 +1.22%p 우위인데
  실제 픽은 거래량 우위). 즉 순위의 '상단'에서 신호가 깨졌다.
- 시사점: 새 축도 풀 평균이 좋다고 채택하면 같은 함정에 빠진다. **조건 ②(픽 기준)가
  실질 관문**이고, 조건 ①(풀 평균)만 통과한 축은 채택하지 않는다.

사전 등록(중요 — 사후 축 고르기 방지):
- **1차 가설은 `foreign_netbuy`(외인 순매수) 하나.** 판정은 이 축으로만 내린다.
- `institution_netbuy` 는 탐색용 2차였으나 09:30 에도 3일 연속 0행이라 수집에서
  제거(2026-08-12, 사전 등록 규칙). `value`(거래대금)는 이미 기각된 대조군.
  둘은 참고 출력이며, 여기서 나온 수치로 "그럼 이 축으로 바꾸자"는 결론을 내지 않는다
  (축을 여러 개 보면 우연히 좋은 축이 하나는 나오게 돼 있다 — 다중비교).
- 판정 조건 3종은 아래 `_verdict_lines()` 에 박아 두고 표본 30일(`MIN_DAYS_FOR_VERDICT`)에
  자동 출력. 15일이 아닌 근거는 그 상수 위 주석 참조(검정력·통계량 표류).
- **조기 중단 규칙**: 수집 1~3일차의 실측 겹침(각 일자 줄의 "겹침" 표기)이 70%를 넘으면
  30일을 채우지 말고 중단한다. TR 선정 근거였던 겹침 20%는 휴장일 mock 의 *전일 종일*
  누적 응답 1회 관측이고, 실제 수집은 09:02(개장 2분) — 조건이 달라 뒤집힐 수 있다.

한계(정직 고지):
- 수익률은 buy-and-hold 근사 — 실제 출구 규칙(트레일링 등)과 다르다. 여기서 재는 건
  발굴 '원재료'이지 전략 손익이 아니다. 왕복비용 ~0.33% 를 넘어야 실전 의미.
- 순매수 축의 풀 순서는 '시장 내 순위 인터리브' 병합 결과(market/rank.py 참조).
  레코드에 market/rank/metric 이 남아 있으므로 다른 병합 규칙 재구성도 가능하다.
- 휴장일 배제: 휴장이어도 수집 잡이 돌면 프로즌 스냅샷(전 종목 0.00%)이 쌓임 —
  전 종목 수익률이 정확히 0 인 날은 표본에서 자동 배제.
- 스냅샷 결손은 종목-일 단위로 스킵하고 건수를 표기 — 조용히 빠지지 않게.

수집 라운드와 판정 표본 (2026-08-12 재등록 — 사유는 ROUND_VERDICT 주석):
- **판정 표본 = 0930 라운드**, 수익률 창 09:40→15:15. ka10065 는 09:02/09:10/09:20 에
  구조적으로 0행이라(2026-08-10~12 실측) 원래 등록한 09:02 표본은 영원히 비고,
  09:30 형성 신호를 09:10 진입 창으로 재면 신호가 생기기 전 가격을 쓰는
  전방주시(look-ahead)가 된다. 순매수 풀 종목의 스냅샷도 0930 레코드 이후(≈09:40)부터
  쌓이므로 09:40 이 데이터가 지지하는 최초 진입 시각이다.
- 09:02(발굴 시각) 레코드는 계속 읽되 진단·대조군 전용 — 실제 매매가 일어나는 시각의
  기록이고, 거래대금 대조군의 v1 이력과 이어진다.
- 채택돼도 곧바로 발굴 소스 교체가 아니다: 매매는 09:02 에 일어나므로 **발굴 시각을
  09:30+ 로 옮기는 반사실 검증**을 별도로 통과해야 한다(사전 등록 유지).

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
ENTRY_MIN = 9 * 60 + 10  # 09:10 — 실제 봇 첫 진입 창(09:02 발굴 라운드 진단용)
# 판정 표본(0930 라운드)의 진입 창. 09:30 에 형성되는 신호로 09:10 진입 수익률을 재면
# 신호가 존재하기 전 가격을 쓰는 전방주시 편향이고, 순매수 풀 종목의 10분 스냅샷도
# 0930 레코드가 쓰인 뒤부터 존재해 09:10 가격 자체가 없다(전부 결손 스킵됨).
ENTRY_MIN_VERDICT = 9 * 60 + 40
EXIT_MIN = 15 * 60 + 15  # 15:15 — EOD 청산 시각
TOL_MIN = 12  # 스냅샷 10분 캐던스 — 목표 시각에서 이 이상 멀면 결손 처리
TOP_N = 3  # 실제 픽 크기
ROUND_TRIP_COST_PCT = 0.33  # 왕복비용 근사 (reports/daily COST_* 합)
# 표본 문턱 30일 (2026-08-10 상향, 사용자 결정). 15일이 아닌 이유는 검정력:
# 실측 일별 차 sd 2.34%p 기준 n=15 는 평균 차 +1.21%p(왕복비용의 3.7배)가 있어야
# |t|>=2 를 넘어 진짜 엣지가 있어도 통과 확률 16~32%였다. n=30 이면 +0.85%p 로 낮아진다.
# 더 결정적으로, 기각된 거래대금 축의 t 는 n=8 +2.14 -> n=12 +0.82 -> n=15 +1.77 ->
# n=20 +2.24 로 표류했다 — 15일 판정은 통계량이 안정되기 전에 동전을 던지는 셈이었다.
MIN_DAYS_FOR_VERDICT = 30
# 겹침은 자카드(교집합/합집합)로 잰다. 예전처럼 대안 풀 크기로만 나누면 기준 풀이
# 진입일에 잘려 있을 때 상한이 50%로 눌려 조건 ③ 이 구조적으로 항상 통과했다.
OVERLAP_MAX = 0.7

ROUND_DISCOVERY = "0902"  # 발굴 시각 기록 — 진단·거래대금 대조군 전용
# 판정 표본 라운드 (2026-08-12 재등록). 사전 등록(2026-08-09) 시점엔 09:02 였으나
# 운영 실측(08-10~13)에서 ka10065 가 09:02·09:10·09:20 에 전부 0행 — TR 이
# 그 시각엔 데이터를 만들지 않아 표본이 영원히 0 이었고 판정 자체가 불가능했다.
# 신호가 형성되는 최소 시각인 09:30 라운드를 표본으로 쓰되, 수익률 창은
# ENTRY_MIN_VERDICT(09:40) 부터 — 형성 이전 가격 사용(look-ahead) 금지.
# 09:10/09:20 탐색 라운드는 3거래일 연속 0건(08-11~13)으로 종료 규칙이 발동해
# 수집 제거(2026-08-13). 과거 레코드는 파일에 남아 커버리지 표에 계속 나온다.
ROUND_VERDICT = "0930"
ROUND_LATE = "0930"  # 하위 호환

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
    round_label: str  # "0930"(판정 표본) / "0902"(발굴 시각, 진단) / "0910"·"0920"(과거 탐색분)
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
    # v2 는 발굴 후보 전체를 rows 로 남긴다 — 진입일에 잘린 volume_pool 보다 이쪽이 정확.
    volume_pool = _pool_symbols(rec.get("volume_pool_rows")) or [
        str(s) for s in rec.get("volume_pool") or []
    ]
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
    # 모든 축이 '그 신호로 뽑았다면' 의 원시 상위 TOP_N — 기준축도 동일 조건.
    # 실제 매수 종목(chosen)을 쓰면 기준축만 게이트·회피 필터를 통과한 셈이라
    # 사과 대 오렌지가 된다(기준축에 유리한 편향). chosen 은 blocked 판정용으로만 사용.
    picks = {name: pool[:TOP_N] for name, pool in pools.items()}
    # round 가 없는 과거 레코드(v1·초기 v2)는 전부 발굴 시각 수집분이다.
    round_label = str(rec.get("round") or ROUND_DISCOVERY)
    return DayRecord(
        date=str(rec.get("date")),
        round_label=round_label,
        pools=pools,
        picks=picks,
        # 늦은 라운드(0910~0930)는 volume_chosen 이 항상 빈 리스트(그 시각엔 매수
        # 개념이 없음) — 진입일까지 '차단일' 로 오인하지 않게 발굴 라운드에서만 판정.
        blocked=not chosen and round_label == ROUND_DISCOVERY,
    )


def load_day_records(
    path: Path = ALT_RANK_PATH, *, round_label: str | None = ROUND_VERDICT
) -> list[DayRecord]:
    """alt-rank JSONL → DayRecord 목록. `round_label=None` 이면 라운드 무관 전부."""
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
        if parsed is not None and (round_label is None or parsed.round_label == round_label):
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


def window_return_pct(
    snaps: TopcapSnapshots, symbol: str, *, entry_min: int = ENTRY_MIN
) -> float | None:
    entry = price_at(snaps, symbol, entry_min)
    exit_ = price_at(snaps, symbol, EXIT_MIN)
    if entry is None or exit_ is None or entry <= 0:
        return None
    return (exit_ / entry - 1.0) * 100.0


def kospi_return_pct(
    date: str, root: Path = SNAPSHOT_ROOT, *, entry_min: int = ENTRY_MIN
) -> float | None:
    """같은 창(entry_min→15:15)의 KOSPI 지수 변화율 — 시장 통제군."""
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

    entry, exit_ = nearest(entry_min), nearest(EXIT_MIN)
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
    # 조건 ② 는 반드시 **같은 날끼리** 비교해야 한다 — 축마다 수집 시작일이 달라
    # 전체 풀링 평균을 빼면 시장 국면 차이가 그대로 '효과' 로 둔갑한다(적대적 검토
    # 실측: 픽이 글자 그대로 동일한데도 -3.18%p 가 나옴). 그래서 일자별로 보관한다.
    day_pick_mean: dict[str, float] = field(default_factory=dict)
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
            "   판정 표본은 스냅샷 잡의 0930 라운드가 쌓아 갑니다 (레코드가 쌓이면 판정으로 바뀜).",
        ]

    common = sorted(set(primary.day_pool_mean) & set(base.day_pool_mean))
    diffs = [primary.day_pool_mean[d] - base.day_pool_mean[d] for d in common]
    t = paired_t(diffs)
    mean_diff = statistics.fmean(diffs) if diffs else 0.0
    pos = sum(1 for d in diffs if d > 0)
    # 픽도 공통일 페어드로 — 전체 풀링 평균 차는 날짜 집합 불일치를 효과로 오인한다.
    pick_common = sorted(set(primary.day_pick_mean) & set(base.day_pick_mean))
    pick_diffs = [primary.day_pick_mean[d] - base.day_pick_mean[d] for d in pick_common]
    pick_gap = statistics.fmean(pick_diffs) if pick_diffs else 0.0
    overlap = (
        statistics.fmean(primary.overlap_with_baseline) if primary.overlap_with_baseline else 0
    )

    cond1 = t is not None and abs(t) >= 2.0 and mean_diff > 0
    cond2 = pick_gap >= ROUND_TRIP_COST_PCT
    cond3 = overlap <= OVERLAP_MAX

    out.append(
        f"판정 대상(1차 가설): {_label(PRIMARY_AXIS)} vs {_label(BASELINE)}"
        f" · 0930 라운드 공통 {len(diffs)}일"
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
        f"{'✅' if cond2 else '❌'} {pick_gap:+.2f}%p (공통 {len(pick_diffs)}일 페어드)"
    )
    out.append(
        f"  ③ 풀이 실제로 다름(자카드 겹침 ≤{OVERLAP_MAX:.0%}): "
        f"{'✅' if cond3 else '❌'} 겹침 {overlap:.0%}"
    )
    if len(diffs) < MIN_DAYS_FOR_VERDICT:
        return out  # 표본 미달이면 채택/기각 문구를 아예 내지 않는다(조기 결론 방지).
    if cond1 and cond2 and cond3:
        out.append("→ **채택 후보** — 발굴 소스 교체를 검토할 근거가 처음으로 생김.")
        out.append(
            "   단, 교체 전 반사실 2종을 통과해야 한다: ① 출구 규칙 반영 손익"
            " ② 발굴 시각을 09:30+ 로 옮기는 검증(신호가 09:02 발굴로는 못 쓰는 시각에 형성)."
        )
    else:
        out.append(
            "→ **기각** — 현행 거래량 순위 유지. 미충족 조건이 하나라도 있으면 교체하지 않는다."
        )
    return out


def analyze(
    records: list[DayRecord],
    *,
    snapshot_root: Path = SNAPSHOT_ROOT,
    entry_min: int = ENTRY_MIN,
) -> dict:
    stats: dict[str, AxisStats] = {}
    skipped_symbol_days = 0
    blocked_days = 0
    holiday_days: list[str] = []
    evaluated_days = 0
    lines: list[str] = []

    for rec in records:
        snaps = parse_topcap_for_date(rec.date, root=snapshot_root)
        kospi = kospi_return_pct(rec.date, root=snapshot_root, entry_min=entry_min)

        universe: set[str] = set()
        for pool in rec.pools.values():
            universe |= set(pool)
        for pick in rec.picks.values():
            universe |= set(pick)
        ret = {sym: window_return_pct(snaps, sym, entry_min=entry_min) for sym in universe}

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
            day_picks: list[float] = []
            for sym in rec.picks.get(axis, []):
                r = ret.get(sym)
                if r is not None:
                    day_picks.append(r)
                    st.pick_returns.append(r)
            if day_returns:
                st.day_pool_mean[rec.date] = statistics.fmean(day_returns)
            if day_picks:
                st.day_pick_mean[rec.date] = statistics.fmean(day_picks)
            union = set(pool) | base_pool
            jac = len(set(pool) & base_pool) / len(union) if union else 0.0
            if axis != BASELINE and pool:
                st.overlap_with_baseline.append(jac)
            ov = "" if axis == BASELINE else f" 겹침 {jac:.0%}"
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
    records = load_day_records(round_label=ROUND_VERDICT)
    if not records:
        print(f"데이터 없음: {ALT_RANK_PATH} 에 0930 라운드 레코드가 없습니다.")
        print("스냅샷 잡이 장중 09:30 창에서 하루 1줄씩 쌓습니다. 며칠 후 재실행하세요.")
        return

    res = analyze(records, entry_min=ENTRY_MIN_VERDICT)
    stats: dict[str, AxisStats] = res["stats"]
    print(
        f"=== 발굴 신호 A/B (판정 표본 0930 라운드 · 창 09:40→15:15,"
        f" 왕복비용 {ROUND_TRIP_COST_PCT}%) ==="
    )
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
    _print_round_coverage()
    _print_discovery_round_diagnostic()


def _print_round_coverage() -> None:
    """라운드별 축 수집 성공률 — "신호가 언제부터 존재하나" 에 대한 직접적인 답.

    탐색 결론(2026-08-13 종결): 09:02·09:10·09:20 은 0행, **최소 형성 시각 = 09:30**
    (09:10/09:20 은 3거래일 연속 0건으로 수집 제거). 그래서 신호는 봇의 진입 창(09:10+)
    초반엔 존재하지 않고, 채택 시 발굴 시각 자체를 옮기는 별도 검증이 필요하다.
    표는 과거 탐색분을 포함한 전체 이력이며, 0930 커버리지가 판정 표본 축적 현황이다.
    """
    rounds: dict[str, dict[str, int]] = {}
    days_per_round: dict[str, set[str]] = {}
    if not ALT_RANK_PATH.exists():
        return
    for line in ALT_RANK_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        label = str(rec.get("round") or ROUND_DISCOVERY)
        day = str(rec.get("date") or "")
        if not day:
            continue
        days_per_round.setdefault(label, set()).add(day)
        axes = rec.get("alt_pools")
        present = set(axes) if isinstance(axes, dict) else set()
        if rec.get("value_pool"):
            present.add("value")
        counts = rounds.setdefault(label, {})
        for axis in present:
            counts[axis] = counts.get(axis, 0) + 1

    if not rounds:
        return
    all_axes = sorted({a for c in rounds.values() for a in c})
    print("\n--- 라운드별 신호 형성 커버리지 (수집일 / 그 라운드 전체일) ---")
    header = f"{'라운드':<8}{'전체':>5}" + "".join(f"{_label(a):>12}" for a in all_axes)
    print(header)
    for label in sorted(rounds):
        total = len(days_per_round.get(label, ()))
        cells = "".join(f"{rounds[label].get(a, 0):>12}" for a in all_axes)
        print(f"{label:<8}{total:>5}{cells}")
    print(
        "→ 최소 형성 시각 = 09:30 (탐색 종결 2026-08-13)."
        " 0930 행의 외인순매수 수집일이 판정 표본 축적 현황이다."
    )


def _print_discovery_round_diagnostic() -> None:
    """발굴 시각(09:02) 라운드 요약 — 판정이 아니라 대조군 진단.

    실제 매매가 일어나는 시각의 기록이라, 판정 표본(0930)과 비교하면 "09:30 우위가
    발굴 시각까지 살아남을 수 있는가" 를 가늠하는 참고가 된다. 순매수 축은 이 시각엔
    구조적으로 없으므로 사실상 거래대금 대조군의 연속 관측이다. **판정에는 쓰지 않는다.**
    """
    discovery = load_day_records(round_label=ROUND_DISCOVERY)
    print()
    if not discovery:
        print("--- 발굴 시각(09:02) 라운드 --- 수집분 없음.")
        return
    res = analyze(discovery, entry_min=ENTRY_MIN)
    stats: dict[str, AxisStats] = res["stats"]
    print(
        f"--- 발굴 시각(09:02) 라운드 · {res['evaluated_days']}일 ·"
        " 창 09:10→15:15 (판정에는 미사용) ---"
    )
    for axis, st in stats.items():
        print(f"{_label(axis):<12} 풀 전체: {describe(st.pool_returns)}")


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
    # 픽은 모든 축이 '원시 풀 상위 TOP_N' — 기준축도 실제 매수 종목이 아니라 풀 상위.
    assert rec.picks[BASELINE] == ["A", "B", "C"]
    assert rec.picks["foreign_netbuy"] == ["X", "A"]
    assert not rec.blocked

    blocked = parse_record({**v2, "volume_chosen": []})
    assert blocked is not None and blocked.blocked
    assert blocked.picks[BASELINE] == ["A", "B", "C"]  # 차단 여부와 무관하게 동일

    # v2 의 volume_pool_rows 가 있으면 (진입일에 잘린) volume_pool 보다 우선한다.
    truncated = parse_record(
        {
            **v2,
            "volume_pool": ["A", "B"],
            "volume_pool_rows": [{"symbol": s_} for s_ in ["A", "B", "C", "D"]],
        }
    )
    assert truncated is not None and truncated.pools[BASELINE] == ["A", "B", "C", "D"]

    # 라운드 필터 — 판정 표본은 0930. round 없는 과거 레코드는 발굴 라운드로 간주.
    assert rec.round_label == ROUND_DISCOVERY
    late = parse_record({**v2, "round": ROUND_VERDICT})
    assert late is not None and late.round_label == ROUND_VERDICT
    # 늦은 라운드는 volume_chosen 이 항상 비므로 차단일로 간주하면 안 된다.
    late_empty = parse_record({**v2, "round": ROUND_VERDICT, "volume_chosen": []})
    assert late_empty is not None and not late_empty.blocked

    v1 = {"v": 1, "date": "2026-07-06", "volume_pool": ["A"], "value_pool": [{"symbol": "Z"}]}
    rec1 = parse_record(v1)
    assert rec1 is not None and rec1.pools["value"] == ["Z"]

    assert parse_record({"volume_pool": ["A"]}) is None  # 대안 축 없으면 비교 불가
    assert paired_t([1.0, 1.0, 1.0]) is None  # 분산 0
    assert paired_t([1.0, 2.0]) is None  # 표본 부족
    t = paired_t([1.0, 2.0, 3.0])
    assert t is not None and t > 0

    # 조건 ② 는 공통일 페어드여야 한다 — 날짜가 안 겹치면 0 이 나와야 정상.
    disjoint = {
        BASELINE: AxisStats(day_pick_mean={"d1": 5.0}, day_pool_mean={"d1": 0.0}),
        PRIMARY_AXIS: AxisStats(day_pick_mean={"d2": 5.0}, day_pool_mean={"d2": 1.0}),
    }
    line2 = next(x for x in _verdict_lines(disjoint) if "②" in x)
    assert "+0.00%p" in line2 and "공통 0일" in line2, line2

    st = {
        BASELINE: AxisStats(
            pick_returns=[0.0], day_pool_mean={"d1": 0.0}, day_pick_mean={"d1": 0.0}
        ),
        PRIMARY_AXIS: AxisStats(
            pick_returns=[1.0], day_pool_mean={"d1": 1.0}, day_pick_mean={"d1": 1.0}
        ),
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
