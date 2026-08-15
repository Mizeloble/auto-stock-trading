"""2단(분봉) 검증대 — 봇의 실제 풀을 복원해 장중 지표를 판정 (read-only).

무엇/왜: 1단 검증대(`indicator_harness.py`, 일봉 502일)는 표본은 크지만 풀을
**D-1 일 거래량**으로 대용했고, 2026-08-15 적대적 검증에서 봇 실제 풀과 자카드
겹침이 **13.2%** 뿐임이 드러났다 — 절대 수익률을 봇 성적으로 읽을 수 없다는 뜻.
이 도구는 그 결함을 정면으로 메운다: 분봉(ka10080)으로 **09:00~09:02 누적 거래량**을
재구성해 봇이 실제로 쓰는 순위(ka10030 거래량 상위)를 그대로 복원한다.

복원이 맞는지 먼저 증명한다: 봇이 실제 풀을 기록해 둔 날과 대조해 겹침을 재고
(`--validate`), 그 겹침이 충분히 높을 때만 이 위에서 지표를 판정한다.
**검증 없이 판정부터 하지 않는다** — 1단에서 그 순서를 어겨 이틀을 잃었다.

🚨 **그 검증이 예상 밖의 것을 잡아냈다 (2026-08-15).** 복원 겹침은 14.2% 로 여전히
낮았는데, 원인은 이 도구의 버그가 아니라 **봇의 순위 데이터 자체가 실제 시장
거래량이 아니라는 것**이었다. 봇이 기록한 09:02 누적 거래량을 실제 당일 총거래량과
대조하니 **50%(25/50건)가 당일 총량을 초과** — 물리적으로 불가능한 값이다
(예: 위메이드맥스 2026-08-14 봇 4,943,263 vs 실제 당일 705,947 = 700%).
키움 자체 일봉·분봉과 fdr 은 서로 정확히 일치하므로 **차트 TR = 실제, 순위
TR(ka10030, mock) = 실제 아님**. 체결가는 실제와 1틱 내외로 일치한다.

따라서 이 도구가 만드는 풀은 **"실거래였다면 뽑혔을 진짜 시장 거래량 상위"** 이고,
봇의 mock 기록과 다른 것이 정상이다. 실거래 전환 판단에는 오히려 이쪽이 맞는 표본이다.

⚠️ **아직 판정에 쓰지 말 것**(2026-08-15 현재). 표본이 14거래일뿐이고, mock 픽 대비
비교를 4일로 시도했더니 동전주 한 종목(시스웍 269620, 09:10 50원 -> 15:15 26원 = -48%)이
결과를 좌우했다. 거래량(주식 수) 순위는 구조적으로 저가주를 상위로 올리므로 이런
단일 종목 지배가 흔하다 — 표본을 충분히 키우기 전엔 방향조차 말할 수 없다.
(봇 실제 풀에도 26원 종목이 들어 있으므로 '봇은 동전주를 안 산다' 는 가정도 틀렸다.)

비용·표본의 트레이드오프(정직 고지):
- 분봉은 종목마다 1요청/페이지다. 334종목 x N페이지 = 수천 요청이라 시간이 든다.
- 조회 범위도 종목마다 다르다(실측: 삼성전자 95 / 동화기업 103 / 위메이드맥스 112
  거래일, **신규 상장은 상장일이 상한** — 딜리셔스 3일). 즉 표본 상한이 ~100일이라
  1단(502일)보다 **작다**. 탐지 하한은 대략 sqrt(502/100) ~ 2.2배 나빠진다.
- 그래도 이쪽이 옳다: 13% 겹침으로 502일을 정밀하게 재봐야 **다른 것을 재는 것**이다.

사용:
  uv run python scripts/diagnostics/intraday_harness.py --fetch --pages 5   # 수집(재개 가능)
  uv run python scripts/diagnostics/intraday_harness.py --validate          # 복원 정확도 검증
  uv run python scripts/diagnostics/intraday_harness.py --selftest
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from auto_trading._common import Bar
from auto_trading.kiwoom.rest import KiwoomRestClient
from auto_trading.market.chart import get_minute_bars

CACHE_DIR = Path("data/studies/cache/minute")
REPORTS = Path("data/reports")
ALT_RANK = Path("data/discovery/alt-rank.jsonl")
SNAPSHOTS = Path("data/snapshots")

POOL_N = 10  # 봇 발굴 후보 풀 크기
MIN_COVERAGE = 0.8  # 그날 데이터 보유 종목이 최다일의 이 비율 미만이면 순위 산출 불가
RANK_CUTOFF_MIN = 9 * 60 + 2  # 09:02 — 봇이 ka10030 을 부르는 시각
ENTRY_MIN = 9 * 60 + 10  # 09:10 — 봇의 첫 진입 창
EXIT_MIN = 15 * 60 + 15  # 15:15 — EOD 청산
OPEN_MIN = 9 * 60  # 09:00 — 시가 단일가 물량이 이 봉에 들어온다
CSV_HEADER = "date,vol02,px02,px10,px1515,vwap10,day_vol"


@dataclass(frozen=True)
class DaySummary:
    """하루치 요약 — 풀 복원(vol02) + 결과 측정(px10/px1515) + VWAP 지표 재료."""

    day: date
    vol02: int  # 09:00~09:02 누적 거래량 (봇의 순위 기준)
    px02: float
    px10: float
    px1515: float
    vwap10: float  # 09:00~09:10 거래량가중평균가
    day_vol: int


def _minute_of(ts: datetime) -> int:
    return ts.hour * 60 + ts.minute


def summarize_day(bars: list[Bar]) -> DaySummary | None:
    """그날 분봉 -> 요약. **불완전한 날은 None** (페이지 경계에서 잘린 날 제외).

    왜 이 가드가 필수인가: 페이지네이션은 최신부터 과거로 오므로 **가장 오래된 날은
    개장 구간이 잘려 있다.** 그대로 쓰면 vol02 가 과소 집계돼 순위가 통째로 틀어진다.
    """
    if not bars:
        return None
    rows = sorted(bars, key=lambda b: b.ts)
    if _minute_of(rows[0].ts) > OPEN_MIN:
        return None  # 개장 봉 결손 — 이 날은 못 쓴다
    vol02 = sum(b.volume for b in rows if _minute_of(b.ts) <= RANK_CUTOFF_MIN)
    if vol02 <= 0:
        return None

    def close_at(target: int) -> float | None:
        cand = [b for b in rows if _minute_of(b.ts) <= target]
        return cand[-1].close if cand else None

    px02, px10, px1515 = close_at(RANK_CUTOFF_MIN), close_at(ENTRY_MIN), close_at(EXIT_MIN)
    if not px02 or not px10 or not px1515:
        return None
    early = [b for b in rows if _minute_of(b.ts) <= ENTRY_MIN and b.volume > 0]
    pv = sum(((b.high + b.low + b.close) / 3) * b.volume for b in early)
    vol_e = sum(b.volume for b in early)
    if vol_e <= 0:
        return None
    return DaySummary(
        day=rows[0].ts.date(),
        vol02=vol02,
        px02=px02,
        px10=px10,
        px1515=px1515,
        vwap10=pv / vol_e,
        day_vol=sum(b.volume for b in rows),
    )


def _cache_path(symbol: str) -> Path:
    return CACHE_DIR / f"{symbol}.csv"


def load_cache(symbol: str) -> dict[date, DaySummary]:
    path = _cache_path(symbol)
    if not path.exists():
        return {}
    out: dict[date, DaySummary] = {}
    for line in path.read_text(encoding="utf-8").splitlines()[1:]:
        p = line.split(",")
        if len(p) != 7:
            continue
        try:
            y, m, d = (int(x) for x in p[0].split("-"))
            out[date(y, m, d)] = DaySummary(
                date(y, m, d),
                int(p[1]),
                float(p[2]),
                float(p[3]),
                float(p[4]),
                float(p[5]),
                int(p[6]),
            )
        except ValueError:
            continue
    return out


def save_cache(symbol: str, days: dict[date, DaySummary]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    lines = [CSV_HEADER]
    for d in sorted(days):
        s = days[d]
        lines.append(
            f"{d.isoformat()},{s.vol02},{s.px02},{s.px10},{s.px1515},{s.vwap10:.4f},{s.day_vol}"
        )
    _cache_path(symbol).write_text("\n".join(lines), encoding="utf-8")


def build_universe() -> list[str]:
    """1단과 동일한 후보 모집단 — 봇이 본 종목 + 시총 상위."""
    syms: set[str] = set()
    for p in sorted(REPORTS.glob("daily-*.json")):
        try:
            d = json.loads(p.read_text())
        except OSError, json.JSONDecodeError:
            continue
        syms.update(str(s) for s in (d.get("rank_candidates") or []))
        syms.update(str(b["symbol"]) for b in (d.get("buys") or []) if b.get("symbol"))
    if ALT_RANK.exists():
        for line in ALT_RANK.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            syms.update(str(s) for s in (rec.get("volume_pool") or []))
            for pool in (rec.get("alt_pools") or {}).values():
                if isinstance(pool, list):
                    syms.update(
                        str(r["symbol"]) for r in pool if isinstance(r, dict) and r.get("symbol")
                    )
    files = sorted(SNAPSHOTS.glob("universe-*.json"))
    if files:
        try:
            top = json.loads(files[-1].read_text())
            syms.update(str(e["symbol"]) for e in top.get("entries") or [] if e.get("symbol"))
        except OSError, json.JSONDecodeError, KeyError:
            pass
    return sorted(s for s in syms if s.isdigit() and len(s) == 6)


async def fetch_all(symbols: list[str], *, pages: int, interval: float) -> None:
    """분봉 수집 — 종목별 캐시에 **병합**(기존 날짜 보존, 새 날짜 추가). 재개 가능."""
    done = 0
    async with KiwoomRestClient(min_request_interval_sec=interval) as client:
        for i, sym in enumerate(symbols, 1):
            cached = load_cache(sym)
            try:
                bars = await get_minute_bars(client, sym, max_pages=pages)
            except Exception as exc:  # 한 종목 실패가 전체를 막지 않게
                print(f"  [{i}/{len(symbols)}] {sym} 실패: {type(exc).__name__}", file=sys.stderr)
                continue
            by_day: dict[date, list[Bar]] = defaultdict(list)
            for b in bars:
                by_day[b.ts.date()].append(b)
            for day, rows in by_day.items():
                s = summarize_day(rows)
                if s is not None and day not in cached:
                    cached[day] = s
            if cached:
                save_cache(sym, cached)
                done += 1
            if i % 25 == 0:
                print(f"  ... {i}/{len(symbols)} 종목 (캐시 보유 {done}) ", file=sys.stderr)
    print(f"수집 완료 — 캐시 보유 종목 {done}/{len(symbols)}", file=sys.stderr)


def load_all(symbols: list[str]) -> dict[str, dict[date, DaySummary]]:
    """종목 -> 일자 -> 요약. 파일을 한 번만 읽고 이후 전 단계가 공유한다."""
    panel: dict[str, dict[date, DaySummary]] = {}
    for sym in symbols:
        days = load_cache(sym)
        if days:
            panel[sym] = days
    return panel


def reconstruct_pools(
    panel: dict[str, dict[date, DaySummary]], *, pool_n: int = POOL_N
) -> dict[str, list[str]]:
    """일자 -> 09:02 누적 거래량 상위 pool_n (봇의 ka10030 순위 복원).

    반환 리스트의 **순서 자체가 거래량 순위**라 `[:3]` 이 봇의 top3 에 대응한다.
    """
    per_day: dict[date, list[tuple[int, str]]] = defaultdict(list)
    for sym, days in panel.items():
        for day, s in days.items():
            per_day[day].append((s.vol02, sym))
    if not per_day:
        return {}
    # 커버리지 가드(2026-08-15 자체 검증에서 발견한 결함): 분봉 조회 범위는 종목마다
    # 달라, 오래된 날일수록 **일부 종목만** 데이터가 있다. 그런 날의 '상위 10' 은
    # 시장 순위가 아니라 '내가 가진 소수 중 상위' 라 완전히 다른 것이 된다.
    # 그날 데이터 보유 종목 수가 최다일 대비 MIN_COVERAGE 미만이면 그 날은 버린다.
    best = max(len(rows) for rows in per_day.values())
    out: dict[str, list[str]] = {}
    for day, rows in per_day.items():
        if len(rows) < pool_n or len(rows) < best * MIN_COVERAGE:
            continue
        rows.sort(key=lambda r: -r[0])
        out[day.isoformat()] = [s for _, s in rows[:pool_n]]
    return out


def actual_pools() -> dict[str, list[str]]:
    """봇이 그날 실제로 기록한 발굴 후보 풀(09:02 라운드)."""
    pools: dict[str, list[str]] = {}
    for rp in sorted(REPORTS.glob("daily-*.json")):
        try:
            d = json.loads(rp.read_text())
        except OSError, json.JSONDecodeError:
            continue
        if d.get("rank_candidates"):
            pools[d["date"]] = [str(s) for s in d["rank_candidates"]]
    if ALT_RANK.exists():
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
            rows = rec.get("volume_pool_rows")
            syms = (
                [str(r["symbol"]) for r in rows if isinstance(r, dict) and r.get("symbol")]
                if isinstance(rows, list)
                else [str(s) for s in rec.get("volume_pool") or []]
            )
            if syms:
                pools[str(rec.get("date"))] = syms
    return pools


def validate(panel: dict[str, dict[date, DaySummary]], *, quiet: bool = False) -> float | None:
    """복원 정확도 — 봇 실제 풀과 겹침. 이게 높아야 이 검증대를 쓸 수 있다.

    반환: 평균 자카드 겹침(공통일 없으면 None). `evaluate` 가 이 값으로 게이트한다.
    """
    recon, actual = reconstruct_pools(panel), actual_pools()
    common = sorted(set(recon) & set(actual))
    if not quiet:
        print(f"복원 {len(recon)}일 · 봇 기록 {len(actual)}일 · 공통 {len(common)}일\n")
    if not common:
        if not quiet:
            print("공통 일자 없음 — 먼저 --fetch 로 수집하세요.")
        return None
    if not quiet:
        print(f"{'일자':<12}{'겹침':>7}{'공통':>6}{'유니버스 커버':>13}   봇 top3 복원")
    jac, top3_hit, coverage = [], [], []
    for day in common:
        a, r = actual[day], set(recon[day])
        sa = set(a)
        j = len(sa & r) / len(sa | r) if sa | r else 0.0
        jac.append(j)
        hits = sum(1 for s in a[:3] if s in r)
        top3_hit.append(hits / min(3, len(a)))
        # 진단: 봇 풀 종목이 애초에 내 캐시에 있었나? (없으면 '유니버스 커버 부족',
        # 있는데 순위에서 밀렸으면 '순위 재현 실패' — 원인이 완전히 다르다)
        d = date.fromisoformat(day)
        present = sum(1 for s in sa if d in panel.get(s, {}))
        coverage.append(present / len(sa) if sa else 0.0)
        if not quiet:
            cov = f"{present}/{len(sa)}"
            print(f"{day:<12}{j:>6.0%}{len(sa & r):>6}{cov:>13}   {hits}/{min(3, len(a))}")
    mj = statistics.fmean(jac)
    mc = statistics.fmean(coverage)
    if not quiet:
        print(f"\n평균 자카드 겹침 : {mj:.1%}   (1단 일봉 대용은 13.2% 였다)")
        print(f"봇 top3 복원율   : {statistics.fmean(top3_hit):.1%}")
        print(f"유니버스 커버율  : {mc:.1%}  (봇 풀 종목이 내 후보/캐시에 존재한 비율)")
        if mj < 0.7:
            print("\n  겹침이 낮은 원인은 **두 가지가 겹쳐 있고, 하나는 고칠 수 없다**:")
            print(
                "  (A) [고칠 수 없음] 봇의 순위 데이터가 실제 시장 거래량이 아니다 —\n"
                "      봇 기록 09:02 누적의 50%가 그날 실제 총거래량을 초과(물리적 불가).\n"
                "      키움 차트 TR·fdr 은 서로 일치하므로 어긋나는 쪽은 순위 TR(mock).\n"
                "      => 이 복원이 봇 mock 기록과 일치할 수 **없다**. 불일치가 정상이다."
            )
            print(
                f"  (B) [고칠 수 있음] 후보 유니버스가 시장 전체를 못 덮는다 (커버 {mc:.0%}).\n"
                "      종목을 넓히면 개선되나 분봉 요청이 종목 수에 비례해 늘어난다."
            )
            print(
                "\n  => 이 도구의 풀은 '실거래였다면 뽑혔을 진짜 거래량 상위' 로 읽어야 하고,\n"
                "     봇 mock 이력과의 겹침으로는 검증할 수 없다. 근거는 분봉이 실제와\n"
                "     일치한다는 교차확인(키움 일봉·fdr)과 집계 로직의 selftest 다."
            )
        if mj >= 0.7:
            print("→ ✅ 복원 성공 — 이 풀 위에서 절대 수익률·지표 판정 가능.")
        elif mj >= 0.4:
            print("→ ⚠️ 부분 복원 — 후보 유니버스가 시장 전체를 못 덮는 탓일 가능성. 상대 비교만.")
        else:
            print("→ ❌ 복원 실패 — 원인 규명 전에는 이 검증대로 판정하지 말 것.")
    return mj


# ── 평가: 복원된 풀 위에서 장중 지표 판정 ────────────────────────────────────
# 특성 규약: fn(요약) -> 점수 | None. 점수가 클수록 선호. **09:10 까지의 정보만** 쓴다.
def _feat_volume(s: DaySummary) -> float | None:
    """현행 기준선 — 09:02 누적 거래량 순위(= 봇의 발굴 순서)."""
    return float(s.vol02)


def _feat_vwap_gap(s: DaySummary) -> float | None:
    """1차 가설 — 09:10 가격이 그때까지의 **진짜 VWAP** 보다 쌀수록 선호.

    2026-08 진입 분해에서 나온 "이미 오른 가격에 산다" 관찰의 직접 검정.
    1단(일봉)에서는 전형가격 대용이라 기각됐지만, 여기서는 장중 실측 VWAP 이다.
    """
    if s.vwap10 <= 0:
        return None
    return -((s.px10 / s.vwap10 - 1) * 100)


INTRADAY_FEATURES = {"volume(현행)": _feat_volume, "vwap_gap(장중)": _feat_vwap_gap}
BASELINE = "volume(현행)"
PRIMARY = "vwap_gap(장중)"
PICK_K = 3
COST_PCT = 0.33


def evaluate(panel: dict[str, dict[date, DaySummary]]) -> None:
    """복원 풀 위에서 특성별 top3 의 09:10->15:15 수익률 비교."""
    from indicator_harness import moving_block_ci  # 같은 폴더 — 부트스트랩 재사용

    overlap = validate(panel, quiet=True)
    recon = reconstruct_pools(panel)
    if overlap is None:
        print("복원 검증 불가(공통 일자 없음) — 판정 생략.")
        return
    print(f"복원 풀 {len(recon)}일 · 봇 풀과 겹침 {overlap:.1%}")
    if overlap < 0.4:
        print("⚠️ 겹침이 낮아 절대 수익률을 봇 성적으로 읽으면 안 된다. 상대 비교만 볼 것.\n")
    else:
        print("")

    per: dict[str, dict[str, float]] = {n: {} for n in INTRADAY_FEATURES}
    pool_mean: dict[str, float] = {}
    for day_s, syms in recon.items():
        day = date.fromisoformat(day_s)
        rows = [(sym, panel[sym][day]) for sym in syms if day in panel.get(sym, {})]
        if len(rows) < PICK_K:
            continue
        ret = {sym: (s.px1515 / s.px10 - 1) * 100 for sym, s in rows if s.px10 > 0}
        if not ret or all(abs(v) < 1e-12 for v in ret.values()):
            continue  # 휴장/프로즌 가드
        pool_mean[day_s] = statistics.fmean(list(ret.values()))
        for name, fn in INTRADAY_FEATURES.items():
            scored = [(sc, sym) for sym, s in rows if (sc := fn(s)) is not None and sym in ret]
            if len(scored) < PICK_K:
                continue
            scored.sort(key=lambda x: -x[0])
            per[name][day_s] = statistics.fmean([ret[s] for _, s in scored[:PICK_K]])

    days = sorted(pool_mean)
    if len(days) < 5:
        print(f"평가 가능일 {len(days)}일 — 표본 부족으로 판정 보류.")
        return
    print(f"평가 {len(days)}거래일 · 창 09:10->15:15 · 픽 {PICK_K} · 왕복비용 {COST_PCT}%\n")
    print(f"{'특성':<18}{'일수':>5}{'일평균':>10}{'비용차감':>10}{'현행 대비':>12}{'95% CI':>20}")
    pv = [pool_mean[d] for d in days]
    print(
        f"{'(참고) 풀 평균':<20}{len(pv):>3}{statistics.fmean(pv):>+9.2f}%{statistics.fmean(pv) - COST_PCT:>+9.2f}%"
    )
    base = per.get(BASELINE, {})
    for name in INTRADAY_FEATURES:
        s = per.get(name, {})
        if not s:
            continue
        ds = sorted(s)
        m = statistics.fmean([s[d] for d in ds])
        line = f"{name:<20}{len(ds):>3}{m:>+9.2f}%{m - COST_PCT:>+9.2f}%"
        if name != BASELINE and base:
            common_d = sorted(set(s) & set(base))
            diffs = [s[d] - base[d] for d in common_d]
            if diffs:
                ci = moving_block_ci(diffs)
                ci_s = f"[{ci[0]:+.2f}, {ci[1]:+.2f}]" if ci else "CI 불가(표본부족)"
                line += f"{statistics.fmean(diffs):>+11.2f}%p{ci_s:>20}"
        print(line)
    print(
        f"\n※ 표본 {len(days)}일은 1단(502일)보다 훨씬 작다 — 탐지 하한이 그만큼 나쁘다."
        "\n  CI 가 0 을 포함하면 '엣지 없음' 이 아니라 **판정 불가**로 읽을 것."
    )


def selftest() -> None:
    """summarize_day 의 결손 가드·집계 로직 검증 (API 없이)."""

    def bar(hh: int, mm: int, vol: int, close: float = 100.0) -> Bar:
        return Bar("X", datetime(2026, 8, 14, hh, mm), close, close + 1, close - 1, close, vol)

    full = [bar(9, m, 10) for m in range(0, 60)] + [
        bar(h, m, 10) for h in (10, 15) for m in (0, 20)
    ]
    s = summarize_day(full)
    assert s is not None
    assert s.vol02 == 30, s.vol02  # 09:00·09:01·09:02 세 봉
    assert s.day_vol == sum(b.volume for b in full)

    # 개장 봉이 없으면(페이지 경계 절단) 반드시 None — 순위 왜곡 방지의 핵심 가드
    assert summarize_day([b for b in full if _minute_of(b.ts) > OPEN_MIN]) is None
    assert summarize_day([]) is None

    # VWAP 은 거래량 가중 — 물량이 쏠린 가격 쪽으로 끌린다
    mixed = [bar(9, 0, 1, 100.0), bar(9, 1, 99, 200.0)]
    s2 = summarize_day(mixed)
    assert s2 is not None
    assert s2.vwap10 > 190, s2.vwap10

    # 캐시 왕복(직렬화 -> 역직렬화) 무손실
    import tempfile

    global CACHE_DIR
    old = CACHE_DIR
    with tempfile.TemporaryDirectory() as td:
        CACHE_DIR = Path(td)
        save_cache("999999", {s.day: s})
        back = load_cache("999999")
        assert back[s.day].vol02 == s.vol02 and back[s.day].px1515 == s.px1515
    CACHE_DIR = old
    print("✅ selftest 통과 — 결손 가드·누적 집계·VWAP 가중·캐시 왕복 정상")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true", help="분봉 수집(재개 가능)")
    ap.add_argument("--validate", action="store_true", help="복원 정확도 검증")
    ap.add_argument("--evaluate", action="store_true", help="복원 풀 위에서 지표 판정")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--pages", type=int, default=5, help="종목당 페이지(1페이지 ~2.4거래일)")
    ap.add_argument("--interval", type=float, default=1.0, help="요청 간격 초")
    ap.add_argument("--limit", type=int, default=0, help="종목 수 제한(시험용)")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return
    universe = build_universe()
    if args.limit:
        universe = universe[: args.limit]
    if args.fetch:
        print(f"수집 시작 — {len(universe)}종목 x {args.pages}페이지 (간격 {args.interval}s)")
        asyncio.run(fetch_all(universe, pages=args.pages, interval=args.interval))
    panel = load_all(universe)
    if args.evaluate:
        evaluate(panel)
    elif args.validate or not args.fetch:
        validate(panel)


if __name__ == "__main__":
    main()
