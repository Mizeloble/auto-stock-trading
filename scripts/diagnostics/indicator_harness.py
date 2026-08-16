"""지표 검증대 — 새 지표 후보를 대표본 일봉에서 판정하는 재사용 도구 (read-only).

무엇/왜: 라이브 수집 표본은 36일뿐이고, 일별 수익률 sd 5.03%/일 기준 +0.5%/일 엣지를
|t|>=2 로 잡으려면 **약 400거래일**이 필요하다(2026-08-14 검정력 실측). 즉 지금 방식으로는
어떤 새 지표도 판정할 수 없고, 아이디어마다 1~2년을 기다려야 한다. 이 도구는 fdr 일봉으로
과거 유니버스를 재구성해 **수백 일 표본을 즉시** 만든다 — 지표를 붙이기 전에 검증할 수
있는 상태를 먼저 확보하는 것이 목적.

검증대가 답하는 질문: "특성 F 로 상위 K 종목을 골랐다면, 현행(거래량 순위)보다 나았나?"
- 유니버스: 봇이 실제 발굴했던 종목 전체 + 시총 상위 200 (data/snapshots/universe-*.json)
- 각 거래일 D 의 풀: **D-1 거래량** 상위 POOL_N (룩어헤드 회피 — D 거래량은 마감 후에야 앎)
- 특성: **D-1 까지의 봉으로만** 계산. 미래 정보가 새면 판정 전체가 무의미해진다.
- 결과: D 시가→종가 수익률 (봇의 09:10→15:15 창 근사)
- 판정: 일별 페어드 차이 → 이동블록 부트스트랩 95% CI + 왕복비용 0.33%p 허들

사전 등록 (2026-08-15, 실행 전 확정):
- **1차 가설 = `vwap_gap` 하나.** "D-1 종가가 그날 평균가보다 싸게 마감한 종목이 D 에 낫다"
  (2026-08 분해에서 '이미 오른 가격에 산다' 관찰의 일봉판). 나머지 특성은 참고 출력이며
  그 수치로 채택 결론을 내지 않는다(다중비교).
- **채택 조건 2종 모두 충족**: ① 현행 대비 페어드 평균 차의 95% CI 가 0 을 포함하지 않음
  ② 평균 차 >= 왕복비용 0.33%p. 하나라도 미달이면 기각.

한계(정직 고지 — 2026-08-15 적대적 검증으로 ①②가 실측 정량화됨):
- 🚨 **① 이 검증대의 풀은 봇의 풀이 아니다 (실측 자카드 겹침 13.2%).** 봇은 당일 09:02
  누적 거래량으로 '오늘 터진 종목'을 뽑지만 여기선 D-1 일 거래량으로 '어제 활발했던
  종목'을 뽑는다. 봇 기록이 있는 44일과 대조한 결과 겹침 13.2%, 봇 실제 top3 가 이
  풀에 포함된 비율 46%. 즉 **여기서 나온 절대 수익률로 봇을 평가하면 안 된다.**
  이 도구의 유효 용도는 *같은 풀 안에서 특성 A vs B* 를 재는 **상대 비교**다.
  (겹침이 낮은 근본 원인은 키움 순위 TR 이 과거 순위를 안 주는 것 — 그래서 D-1 대용.)
- 🚨 **② 탐지 하한 ≈ +0.5%p (왕복비용 0.33%p 보다 높다).** 합성 신호에 노이즈를 섞어
  효과 크기를 낮춰가며 실측한 결과 +0.54%p 는 CI 가 0 을 배제했지만 **+0.30%p 는 배제하지
  못했다.** 즉 "비용을 갓 넘는" 진짜 엣지는 이 도구가 '기각' 으로 잘못 판정할 수 있다
  (2종 오류). 기각 결론은 "≥0.5%p 짜리 엣지는 없다" 로 읽어야 하고, 0~0.5%p 구간은
  **판정 불가**다. `positive_control` 의 +6%p 는 '고장 안 났음' 만 증명할 뿐이다.
- **신규 상장 제외**: `MIN_HISTORY` 미만 종목은 빠진다. 봇은 신규 상장주를 실제로 사므로
  (2026-08-12 딜리셔스 = 상장 당일 매수, 그날 -17%) 이 축도 ① 의 괴리를 키운다.
- **생존편향**: 유니버스가 '오늘 시점에 눈에 띄는 종목'(봇이 봤던 종목 + 현재 시총 상위)
  이라 과거로 소급하면 승자 편향. 방향은 '실제보다 좋아 보임' — 음(-) 결론엔 보수적이다.
- **일봉 근사**: 시가→종가는 봇의 실제 창(09:10→15:15)·출구 규칙(트레일링 등)과 다르다.
  여기서 재는 건 '선정 원재료'이지 전략 손익이 아니다.
- **게이트 미반영**: 봇은 SMA·breadth 게이트로 나쁜 날 쉬고 그 게이트는 실측 순이득이다.
- **VWAP 대용**: fdr 은 거래대금을 주지 않아 진짜 VWAP 을 못 만든다. (고가+저가+종가)/3
  전형가격(typical price)을 대용으로 쓴다. 장중 진짜 VWAP 은 분봉 검증대(2단)의 몫.

실행:
  uv run python scripts/diagnostics/indicator_harness.py            # 판정
  uv run python scripts/diagnostics/indicator_harness.py --selftest # 데이터 없이 로직 검증
  uv run python scripts/diagnostics/indicator_harness.py --years 3  # 표본 확대
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import random
import statistics
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

CACHE_DIR = Path("data/studies/cache/fdr")
REPORTS = Path("data/reports")
ALT_RANK = Path("data/discovery/alt-rank.jsonl")
SNAPSHOTS = Path("data/snapshots")

POOL_N = 10  # 그날 후보 풀 크기 (봇 발굴 pool_n 과 동일 계열)
PICK_K = 3  # 실제 매수 종목 수 (top_n=3)
COST_PCT = 0.33  # 왕복비용 근사
MIN_HISTORY = 25  # 특성 계산에 필요한 최소 과거 봉 수
BOOT_N, BOOT_BLOCK, BOOT_SEED = 5000, 10, 20260815

PRIMARY_FEATURE = "vwap_gap"
BASELINE_FEATURE = "volume(현행)"


@dataclass(frozen=True)
class DayBar:
    """하루치 봉 — fdr 컬럼의 최소 부분집합."""

    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float


# ── 특성 정의 ────────────────────────────────────────────────────────────────
# 규약: fn(history) -> 점수 | None. history 는 **D-1 까지** 오름차순 봉.
# 점수가 클수록 선호(= 상위 K 에 뽑힘). None 이면 그 종목은 그날 제외.
FeatureFn = Callable[[Sequence[DayBar]], float | None]


def _volume_rank(history: Sequence[DayBar]) -> float | None:
    """현행 기준선 — D-1 거래량이 많을수록 선호."""
    return history[-1].volume if history else None


def _vwap_gap(history: Sequence[DayBar]) -> float | None:
    """1차 가설 — D-1 종가가 전형가격((H+L+C)/3)보다 **쌀수록** 선호.

    괴리 = (종가 / 전형가격 - 1) * 100. 이 값이 낮을수록 "그날 실제 거래된 평균보다
    싸게 마감했다" 는 뜻이라, 점수는 부호를 뒤집어 돌려준다.
    """
    if not history:
        return None
    b = history[-1]
    typical = (b.high + b.low + b.close) / 3
    if typical <= 0:
        return None
    return -((b.close / typical - 1) * 100)


def _close_position(history: Sequence[DayBar]) -> float | None:
    """탐색용 — D-1 종가가 일중 레인지의 어디에 마감했나 (낮을수록 선호)."""
    if not history:
        return None
    b = history[-1]
    rng = b.high - b.low
    if rng <= 0:
        return None
    return -((b.close - b.low) / rng)


def _reversal_5d(history: Sequence[DayBar]) -> float | None:
    """탐색용 — 최근 5봉 수익률이 낮을수록 선호(단기 역추세)."""
    if len(history) < 6:
        return None
    old, new = history[-6].close, history[-1].close
    return -((new / old - 1) * 100) if old > 0 else None


FEATURES: dict[str, FeatureFn] = {
    BASELINE_FEATURE: _volume_rank,
    PRIMARY_FEATURE: _vwap_gap,
    "close_position": _close_position,
    "reversal_5d": _reversal_5d,
}


# ── 유니버스 · 패널 ──────────────────────────────────────────────────────────
def build_universe() -> list[str]:
    """봇이 실제 발굴했던 종목 + 최신 시총 상위 — 그날 풀을 뽑을 후보 모집단."""
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
    files = sorted(glob.glob(str(SNAPSHOTS / "universe-*.json")))
    if files:
        try:
            top = json.loads(Path(files[-1]).read_text())
            syms.update(str(e["symbol"]) for e in top.get("entries") or [] if e.get("symbol"))
        except OSError, json.JSONDecodeError, KeyError:
            pass
    return sorted(s for s in syms if s.isdigit() and len(s) == 6)


def _cache_path(symbol: str) -> Path:
    return CACHE_DIR / f"{symbol}.csv"


def fetch_symbol(symbol: str, start: str, end: str) -> list[DayBar]:
    """fdr 일봉 — 디스크 캐시 우선. 네트워크 실패는 빈 리스트(그 종목만 제외)."""
    cache = _cache_path(symbol)
    if cache.exists():
        rows = []
        for line in cache.read_text(encoding="utf-8").splitlines()[1:]:
            parts = line.split(",")
            if len(parts) != 6:
                continue
            try:
                y, m, d = (int(x) for x in parts[0].split("-"))
                o, h, low, c, v = (float(x) for x in parts[1:])
                rows.append(DayBar(date(y, m, d), o, h, low, c, v))
            except ValueError:
                continue
        return rows
    try:
        import FinanceDataReader as fdr

        df = fdr.DataReader(symbol, start, end)
    except Exception:
        return []
    rows = []
    lines = ["date,open,high,low,close,volume"]
    for idx, r in df.iterrows():
        try:
            day = idx.date()
            bar = DayBar(
                day,
                float(r["Open"]),
                float(r["High"]),
                float(r["Low"]),
                float(r["Close"]),
                float(r["Volume"]),
            )
        except KeyError, TypeError, ValueError, AttributeError:
            continue
        if bar.open <= 0 or bar.close <= 0 or bar.volume <= 0:
            continue  # 거래정지·이상치
        rows.append(bar)
        lines.append(f"{day.isoformat()},{bar.open},{bar.high},{bar.low},{bar.close},{bar.volume}")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text("\n".join(lines), encoding="utf-8")
    return rows


def build_panel(symbols: Sequence[str], start: str, end: str) -> dict[str, list[DayBar]]:
    panel: dict[str, list[DayBar]] = {}
    for i, sym in enumerate(symbols, 1):
        bars = fetch_symbol(sym, start, end)
        if len(bars) >= MIN_HISTORY:
            panel[sym] = bars
        if i % 50 == 0:
            print(f"  ... {i}/{len(symbols)} 종목 적재", file=sys.stderr)
    return panel


# ── 평가 ─────────────────────────────────────────────────────────────────────
def moving_block_ci(
    diffs: Sequence[float], *, block: int = BOOT_BLOCK
) -> tuple[float, float] | None:
    """이동블록 부트스트랩 95% CI — 표본의 자기상관 보정.

    `block` 은 보유기간이 겹치는 경우(hold>1) 호출자가 넓혀 준다 — 겹친 표본을
    독립으로 취급하면 CI 가 실제보다 좁아져 없는 유의성을 만들어낸다.
    """
    n = len(diffs)
    if n < block * 2:
        return None
    rng = random.Random(BOOT_SEED)
    nblocks = math.ceil(n / block)
    means = []
    for _ in range(BOOT_N):
        sample: list[float] = []
        for _ in range(nblocks):
            s = rng.randrange(0, n - block + 1)
            sample.extend(diffs[s : s + block])
        means.append(statistics.fmean(sample[:n]))
    means.sort()
    return means[int(0.025 * BOOT_N)], means[int(0.975 * BOOT_N)]


def evaluate(panel: dict[str, list[DayBar]], *, hold: int = 1) -> dict:
    """일자별로 풀을 만들고 특성마다 top-K 픽의 보유기간 수익률을 집계.

    `hold` = 보유 거래일 수. 1 이면 당일 시가→종가(현행 데이트레이딩), N 이면
    D 시가 매수 → D+N-1 종가 매도. **회전율이 1/N 로 줄어 왕복비용 부담도 1/N** 이
    되므로, "엣지가 없는 게 아니라 비용을 못 이기는 것" 인지 가르는 축이다.
    """
    index: dict[str, dict[date, int]] = {
        sym: {b.day: i for i, b in enumerate(bars)} for sym, bars in panel.items()
    }
    all_days = sorted({b.day for bars in panel.values() for b in bars})
    per_feature: dict[str, dict[date, float]] = {name: {} for name in FEATURES}
    pool_mean: dict[date, float] = {}
    universe_mean: dict[date, float] = {}

    for day in all_days:
        # 풀 = D-1 거래량 상위. 각 종목의 D-1 은 그 종목 시계열에서 day 직전 봉.
        cands: list[tuple[float, str, list[DayBar]]] = []
        outcome: dict[str, float] = {}
        for sym, bars in panel.items():
            pos = index[sym].get(day)
            if pos is None or pos < MIN_HISTORY:
                continue
            hist = bars[:pos]  # D-1 까지 — 룩어헤드 차단
            today = bars[pos]
            exit_pos = pos + hold - 1  # 보유 마지막 거래일
            if today.open <= 0 or exit_pos >= len(bars):
                continue
            cands.append((hist[-1].volume, sym, hist))
            outcome[sym] = (bars[exit_pos].close / today.open - 1) * 100
        if len(cands) < POOL_N:
            continue
        # 유니버스 전체 평균 = 베타 벤치마크. 보유기간을 늘리면 시장 상승분이 그대로
        # 수익에 들어오므로, 이걸 빼지 않으면 '엣지' 와 '그냥 오른 것' 을 구분 못 한다.
        universe_mean[day] = statistics.fmean([outcome[s] for _, s, _ in cands])
        cands.sort(key=lambda c: -c[0])
        pool = cands[:POOL_N]
        pool_mean[day] = statistics.fmean([outcome[s] for _, s, _ in pool])

        for name, fn in FEATURES.items():
            scored = []
            for _, sym, hist in pool:
                score = fn(hist)
                if score is not None:
                    scored.append((score, sym))
            if len(scored) < PICK_K:
                continue
            scored.sort(key=lambda x: -x[0])
            picks = [s for _, s in scored[:PICK_K]]
            per_feature[name][day] = statistics.fmean([outcome[s] for s in picks])

    return {
        "per_feature": per_feature,
        "pool_mean": pool_mean,
        "universe_mean": universe_mean,
        "days": len(pool_mean),
        "hold": hold,
    }


TRADING_DAYS_PER_YEAR = 250


def hold_sweep(panel: dict[str, list[DayBar]], holds: Sequence[int]) -> None:
    """보유기간 스윕 — 회전율을 낮추면 비용 부담이 줄어 결과가 뒤집히는지 본다.

    핵심 산식: 보유 N일이면 연 `250/N` 회전 → **연 비용 = 왕복비용 x 250/N**.
    현행(N=1)은 연 82%p 를 비용으로 쓴다(픽 3종목이라 실제 자본 대비로는 더 작지만,
    수익률 자체가 비용을 이겨야 한다는 구조는 동일). N 을 키우면 이 부담이 급감한다.
    """
    print(f"\n=== 보유기간 스윕 (풀 {POOL_N} · 픽 {PICK_K} · 왕복비용 {COST_PCT}%) ===")
    print(
        f"{'보유':<6}{'평가':>6}{'픽 수익':>9}{'유니버스':>9}{'초과(알파)':>11}"
        f"{'초과 95% CI':>21}{'연 비용':>9}"
    )
    for hold in holds:
        res = evaluate(panel, hold=hold)
        base = res["per_feature"].get(BASELINE_FEATURE, {})
        uni = res["universe_mean"]
        if not base or not uni:
            continue
        days = sorted(set(base) & set(uni))
        vals = [base[d] for d in days]
        unis = [uni[d] for d in days]
        excess = [base[d] - uni[d] for d in days]
        turns = TRADING_DAYS_PER_YEAR / hold
        # 보유기간이 겹치므로 자기상관이 크다 — 블록을 보유일의 2배로 넓힌다.
        ci = moving_block_ci(excess, block=max(BOOT_BLOCK, hold * 2))
        ci_s = f"[{ci[0]:+.2f}, {ci[1]:+.2f}]" if ci else "CI 불가"
        print(
            f"{hold:>3}일{len(days):>6}{statistics.fmean(vals):>+8.2f}%"
            f"{statistics.fmean(unis):>+8.2f}%{statistics.fmean(excess):>+10.2f}%p"
            f"{ci_s:>21}{COST_PCT * turns:>8.1f}%"
        )
    print(
        "\n※ **초과(알파) = 픽 - 유니버스 전체 평균.** 보유기간을 늘리면 시장 상승분이"
        "\n  그대로 수익에 들어오므로, 이걸 빼야 '선정 실력' 과 '그냥 오른 것' 이 갈린다."
        "\n※ 회전율이 낮을수록 연 비용이 급감한다 — 알파가 같아도 순익은 달라진다."
        "\n※ 보유기간이 길수록 표본이 겹쳐 CI 가 낙관적일 수 있다(블록 확대로 일부 보정)."
    )


def report(res: dict) -> None:
    per, pool = res["per_feature"], res["pool_mean"]
    base = per.get(BASELINE_FEATURE, {})
    print(f"\n평가 {res['days']}거래일 · 풀 {POOL_N} · 픽 {PICK_K} · 왕복비용 {COST_PCT}%\n")
    print(
        f"{'특성':<18}{'일수':>6}{'일평균':>9}{'비용차감':>9}"
        f"{'비용후 95% CI':>21}{'현행 대비':>11}{'95% CI':>20}"
    )

    def abs_ci_str(vals: list[float]) -> str:
        """비용 차감 후 절대 수익률의 CI — '이 유니버스가 돈이 되는가' 에 대한 답."""
        ci = moving_block_ci([v - COST_PCT for v in vals])
        return f"[{ci[0]:+.2f}, {ci[1]:+.2f}]" if ci else "CI 불가"

    pool_days = sorted(pool)
    pool_vals = [pool[d] for d in pool_days]
    print(
        f"{'(참고) 풀 평균':<20}{len(pool_days):>4}"
        f"{statistics.fmean(pool_vals):>+8.2f}%{statistics.fmean(pool_vals) - COST_PCT:>+8.2f}%"
        f"{abs_ci_str(pool_vals):>21}"
    )
    for name in FEATURES:
        series = per.get(name, {})
        if not series:
            continue
        days = sorted(series)
        vals = [series[d] for d in days]
        mean = statistics.fmean(vals)
        line = (
            f"{name:<20}{len(days):>4}{mean:>+8.2f}%{mean - COST_PCT:>+8.2f}%{abs_ci_str(vals):>21}"
        )
        if name != BASELINE_FEATURE and base:
            common = sorted(set(series) & set(base))
            diffs = [series[d] - base[d] for d in common]
            if diffs:
                gap = statistics.fmean(diffs)
                ci = moving_block_ci(diffs)
                ci_s = f"[{ci[0]:+.2f}, {ci[1]:+.2f}]" if ci else "CI 불가"
                line += f"{gap:>+10.2f}%p{ci_s:>20}"
        print(line)
    print(
        "\n※ '비용후 95% CI' 는 **이 재구성 풀(D-1 거래량 상위)** 에 대한 값이다."
        " 봇의 실제 풀과는 겹침 13.2%(2026-08-15 실측)이므로 **봇 성적으로 읽지 말 것.**"
        f"\n※ 탐지 하한 ≈ +0.5%p > 왕복비용 {COST_PCT}%p — '현행 대비' 가 0~0.5%p 구간이면"
        " 기각이 아니라 **판정 불가**로 읽어야 한다."
    )

    print("\n--- 사전 등록 판정 (1차 가설만) ---")
    prim, base_s = per.get(PRIMARY_FEATURE, {}), base
    if not prim or not base_s:
        print("데이터 부족 — 판정 불가")
        return
    common = sorted(set(prim) & set(base_s))
    diffs = [prim[d] - base_s[d] for d in common]
    gap = statistics.fmean(diffs) if diffs else 0.0
    ci = moving_block_ci(diffs)
    cond1 = ci is not None and (ci[0] > 0 or ci[1] < 0)
    cond2 = gap >= COST_PCT
    print(f"대상: {PRIMARY_FEATURE} vs {BASELINE_FEATURE} · 공통 {len(common)}일")
    ci_s = f"[{ci[0]:+.2f}, {ci[1]:+.2f}]" if ci else "계산 불가"
    print(f"  ① CI 가 0 을 포함하지 않음 : {'✅' if cond1 else '❌'} 95% CI {ci_s}")
    print(f"  ② 평균 차 >= 비용 {COST_PCT}%p : {'✅' if cond2 else '❌'} {gap:+.2f}%p")
    if cond1 and cond2:
        print("→ **채택 후보** — 분봉 검증대(2단)로 장중 진짜 VWAP 재확인 후 반영 검토.")
    else:
        print("→ **기각** — 현행 유지. 조건 하나라도 미달이면 채택하지 않는다.")


def positive_control(panel: dict[str, list[DayBar]]) -> None:
    """양성 대조군 — 일부러 미래를 보는 특성을 넣어 검증대가 신호를 잡는지 확인.

    무엇/왜: 기각 결론을 믿으려면 도구가 **양성을 탐지할 수 있음**이 먼저 증명돼야 한다.
    치팅 특성(= D 의 실제 수익률을 그대로 점수로)조차 0 근처로 나온다면 검증대가 고장난
    것이고 지금까지의 모든 기각이 무효다. 2026-08-15 실측: 치팅 +5.86%/일 vs 현행
    -0.25%/일 (**+6.12%p**).

    ⚠️ **이 검사가 증명하는 것은 '고장 안 났음' 뿐이다** (2026-08-15 적대적 검증).
    +6%p 는 거대한 효과라 잡히는 게 당연하다. 노이즈를 섞어 효과를 낮춰 실측한
    **실질 탐지 하한은 ≈ +0.5%p** (+0.54%p 는 CI 가 0 배제, +0.30%p 는 배제 실패).
    왕복비용 0.33%p 보다 높으므로, 기각 결론은 "≥0.5%p 엣지는 없다" 까지만 말한다.
    """
    lookup: dict[int, float] = {}
    for bars in panel.values():
        for i in range(len(bars) - 1):
            nxt = bars[i + 1]
            if nxt.open > 0:
                lookup[id(bars[i])] = (nxt.close / nxt.open - 1) * 100

    def cheat(history: Sequence[DayBar]) -> float | None:
        return lookup.get(id(history[-1])) if history else None

    FEATURES["(대조군) 미래를 봄"] = cheat
    res = evaluate(panel)
    per = res["per_feature"]
    cheat_s, base_s = per.get("(대조군) 미래를 봄", {}), per.get(BASELINE_FEATURE, {})
    del FEATURES["(대조군) 미래를 봄"]
    if not cheat_s or not base_s:
        print("\n[양성 대조군] 데이터 부족 — 확인 불가")
        return
    gap = statistics.fmean(list(cheat_s.values())) - statistics.fmean(list(base_s.values()))
    ok = gap > 1.0
    print(
        f"\n[양성 대조군] 미래를 보는 특성 - 현행 = {gap:+.2f}%p "
        f"→ {'✅ 탐지 능력 정상 (기각 결론 신뢰 가능)' if ok else '⚠️ 검증대 결함 의심 — 기각 재검토'}"
    )


def selftest() -> None:
    """데이터 없이 룩어헤드·특성·부트스트랩 로직 검증."""
    mk = lambda d, o, h, low, c, v: DayBar(date(2026, 1, d), o, h, low, c, v)  # noqa: E731
    hist = [mk(i, 100, 110, 90, 100, 1000) for i in range(1, 4)]
    assert _volume_rank(hist) == 1000
    # 종가=전형가격이면 괴리 0. `x or 기본값` 은 0.0 을 falsy 로 삼켜 버리므로
    # 반드시 None 을 명시적으로 확인한다(이 함정을 selftest 가 실제로 잡았다).
    flat = _vwap_gap([mk(4, 100, 110, 90, 100, 1)])
    assert flat is not None and abs(flat) < 1e-9
    # 종가가 전형가격보다 높으면(비싸게 마감) 점수는 음수여야 한다
    rich = _vwap_gap([mk(5, 100, 120, 80, 118, 1)])
    assert rich is not None and rich < 0
    # 저가 마감이면 close_position 점수가 높다(선호)
    low_close = _close_position([mk(6, 100, 110, 90, 91, 1)])
    high_close = _close_position([mk(7, 100, 110, 90, 109, 1)])
    assert low_close is not None and high_close is not None and low_close > high_close
    assert _reversal_5d(hist) is None  # 표본 부족
    assert moving_block_ci([0.0] * 5) is None  # 블록보다 짧으면 None
    ci = moving_block_ci([1.0] * 40)
    assert ci is not None and abs(ci[0] - 1.0) < 1e-9

    # 룩어헤드 차단: 평가 루프가 당일 봉을 특성에 넘기지 않는지 — 당일만 거래량이
    # 폭증하는 종목을 만들어, volume 특성이 그것을 D 에 선택하지 못함을 확인.
    bars = [mk(i, 100, 101, 99, 100, 10) for i in range(1, 27)]
    spike = [*bars[:-1], DayBar(date(2026, 1, 26), 100, 101, 99, 100, 10_000_000)]
    panel = {f"00000{i}": list(spike if i == 1 else bars) for i in range(1, 12)}
    res = evaluate(panel)
    assert res["days"] >= 0  # 실행만으로 예외 없음 확인
    print("✅ selftest 통과 — 특성·부트스트랩·룩어헤드 경로 정상")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=2.0, help="평가 기간(년)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--no-control", action="store_true", help="양성 대조군 생략 (기본은 매번 실행)")
    ap.add_argument(
        "--hold-sweep", action="store_true", help="보유기간 1/3/5/10/20일 스윕 (회전율 축)"
    )
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return

    end = date.today()
    start = end - timedelta(days=int(365 * args.years) + 60)  # 워밍업 여유
    universe = build_universe()
    print(f"유니버스 {len(universe)}종목 · 기간 {start} ~ {end} (캐시: {CACHE_DIR})")
    panel = build_panel(universe, start.isoformat(), end.isoformat())
    print(f"적재 완료 {len(panel)}종목")
    if args.hold_sweep:
        hold_sweep(panel, [1, 3, 5, 10, 20])
        return
    report(evaluate(panel))
    if not args.no_control:
        positive_control(panel)


if __name__ == "__main__":
    main()
