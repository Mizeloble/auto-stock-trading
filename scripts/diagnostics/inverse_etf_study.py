"""약세장 인버스 ETF 읽기전용 검증 — "지수 게이트가 막은 날, 인버스를 샀으면?".

무엇/왜:
- 현재 day-trade 는 long-only 라 지수 게이트(`market.index.index_gate`: 종가가
  5/20 SMA 둘 다 위일 때만 통과)가 약세장에서 매수를 막는다. 최근(2026-06-25·29)
  진입 0 의 실제 사유도 `KOSPI_below_sma` 였다. 사용자 질문: "약세장에 뭐라도 —
  기업 주식이 아니더라도 — 투자하고 싶다". 가장 직접적 후보가 **인버스 ETF**
  (KODEX 인버스 114800: 지수가 빠지면 오름)인데, 봇이 거래량 상위에서 일부러
  제외하는 바로 그 종목군이다.
- 감이 아니라 숫자로: "게이트가 long 을 막은 바로 그 약세일에 인버스를 시가 매수→
  종가 청산했으면 평균적으로 벌었나?"를 일봉으로 근사 측정한다.

방식 (룩어헤드 0):
1. fdr 로 KOSPI 종합지수('KS11') 일별 종가 → 각 거래일 D 의 게이트를 **D-1 까지의
   종가**로 판정(last=D-1 종가, sma5/sma20=D-1 기준). 봇은 09:06 에 당일 발생 중
   봉을 보지만, 보수적으로 전일 확정 종가만 써서 룩어헤드를 0 으로 만든다.
2. fdr 로 자산별 일봉 OHLC → 당일 시가→종가 수익률 (close-open)/open.
   - 069500 KODEX 200 (long, 비교군 — 게이트가 막은 이유 확인용)
   - 114800 KODEX 인버스 (지수 -1배)
   - 252670 KODEX 200선물인버스2X (지수 -2배, 변동성 큼)
3. 거래일을 gated(게이트 차단=약세) vs open(게이트 통과)로 나눠 각 자산의 당일
   평균 수익률·승률 비교. 핵심: gated 날의 인버스 평균이 양(+)이고 long 보다 나은가.
4. gated 날 인버스 평균에 이동블록 부트스트랩 CI/p → 0 을 포함하면 결론 보류가 정직.

한계(정직): 시가→종가 근사(장중 타이밍·슬리피지 무시)·비용 제외·인버스 ETF 의
일일 복리붕괴(추적오차)는 일봉 OHLC 에 이미 반영되지만 다일 보유엔 부적합·생존편향
없음(대형 ETF)·게이트 프록시(전일 종가, 봇의 09:06 발생봉과 미세 차이). 방향성
신호일 뿐 증명 아님.

실행: uv run python scripts/diagnostics/inverse_etf_study.py [--years 3] [--self-check]
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

# ── 게이트 상수 (market/index.py 와 일치) ──────────────────────────────────
SMA_SHORT = 5
SMA_LONG = 20

# ── 검증 대상 자산 ─────────────────────────────────────────────────────────
ASSETS: dict[str, str] = {
    "069500": "KODEX 200 (long)",
    "114800": "KODEX 인버스 (-1x)",
    "252670": "KODEX 200선물인버스2X (-2x)",
}
KOSPI_INDEX = "KS11"  # fdr KOSPI 종합지수

BOOT_N = 3_000
BOOT_BLOCK = 5  # 이동블록 길이 (일 수익률 자기상관 보존)

STUDY_DIR = Path("data/studies")


@dataclass
class DayRow:
    """거래일 1건: 게이트 상태 + 각 자산 당일 시가→종가 수익률(%)."""

    d: date
    gated: bool  # True = 게이트 차단(약세, long 매수 불가)
    kospi_close: float
    sma5: float
    sma20: float
    intraday_pct: dict[str, float] = field(default_factory=dict)  # 자산코드 → %


def _gate_above(closes_desc: list[float]) -> tuple[bool, float, float, float]:
    """closes_desc = [최신, ...과거] (index.py 와 동일 순서). last>sma5>0 and last>sma20>0."""
    last = closes_desc[0]
    sma5 = statistics.fmean(closes_desc[:SMA_SHORT])
    sma20 = statistics.fmean(closes_desc[:SMA_LONG])
    above = last > sma5 > 0 and last > sma20 > 0
    return above, last, sma5, sma20


def _bootstrap_ci(samples: list[float]) -> tuple[float, float, float]:
    """이동블록 부트스트랩 → (lo95, hi95, p_two_sided≈P(mean≤0)*2)."""
    n = len(samples)
    if n < BOOT_LONG_MIN:
        return float("nan"), float("nan"), float("nan")
    means: list[float] = []
    n_blocks = (n // BOOT_BLOCK) + 1
    for _ in range(BOOT_N):
        resampled: list[float] = []
        for _b in range(n_blocks):
            start = random.randint(0, n - 1)
            resampled.extend(samples[start : start + BOOT_BLOCK])
        resampled = resampled[:n]
        means.append(statistics.fmean(resampled))
    means.sort()
    lo = means[int(0.025 * BOOT_N)]
    hi = means[int(0.975 * BOOT_N)]
    below = sum(1 for m in means if m <= 0) / BOOT_N
    p = 2 * min(below, 1 - below)
    return lo, hi, p


BOOT_LONG_MIN = 10  # 부트스트랩 최소 표본


def _summary(rows: list[DayRow], code: str) -> dict:
    """한 자산의 gated/open 그룹별 평균·승률 요약."""
    out: dict = {"name": ASSETS.get(code, code)}
    for label, want_gated in (("gated", True), ("open", False)):
        vals = [r.intraday_pct[code] for r in rows if r.gated is want_gated and code in r.intraday_pct]
        if not vals:
            out[label] = {"n": 0}
            continue
        wins = sum(1 for v in vals if v > 0)
        entry = {
            "n": len(vals),
            "mean_pct": round(statistics.fmean(vals), 4),
            "median_pct": round(statistics.median(vals), 4),
            "win_rate": round(wins / len(vals), 3),
            "best_pct": round(max(vals), 3),
            "worst_pct": round(min(vals), 3),
        }
        if want_gated:
            lo, hi, p = _bootstrap_ci(vals)
            entry["boot_ci95"] = [round(lo, 4), round(hi, 4)]
            entry["boot_p"] = round(p, 4)
        out[label] = entry
    return out


def build_rows(years: float) -> list[DayRow]:
    """fdr 로 지수·자산 일봉을 받아 게이트 + 당일 수익률 패널 구성."""
    import FinanceDataReader as fdr  # noqa: PLC0415  (스크립트 로컬 import)

    today = datetime.now(KST).date()
    start = today - timedelta(days=int(365 * years) + 40)  # SMA 워밍업 여유
    start_s = start.strftime("%Y-%m-%d")

    kospi = fdr.DataReader(KOSPI_INDEX, start_s)
    if kospi.empty:
        raise SystemExit("KOSPI 지수 데이터를 받지 못했습니다 (fdr KS11).")
    kospi_closes = [(idx.date(), float(row["Close"])) for idx, row in kospi.iterrows()]

    asset_ohlc: dict[str, dict[date, tuple[float, float]]] = {}
    for code in ASSETS:
        df = fdr.DataReader(code, start_s)
        if df.empty:
            print(f"  ⚠ {code} 데이터 없음 — 건너뜀", file=sys.stderr)
            asset_ohlc[code] = {}
            continue
        asset_ohlc[code] = {
            idx.date(): (float(r["Open"]), float(r["Close"]))
            for idx, r in df.iterrows()
            if float(r["Open"]) > 0
        }

    rows: list[DayRow] = []
    cutoff = today - timedelta(days=int(365 * years))
    # i 번째 거래일 D 의 게이트는 [0..i-1] 종가(최신=i-1)로 판정 — 룩어헤드 0.
    for i in range(SMA_LONG, len(kospi_closes)):
        d, _ = kospi_closes[i]
        if d < cutoff:
            continue
        prior_desc = [c for _, c in reversed(kospi_closes[:i])]  # 최신(i-1) → 과거
        above, last, sma5, sma20 = _gate_above(prior_desc)
        row = DayRow(d=d, gated=not above, kospi_close=last, sma5=sma5, sma20=sma20)
        for code in ASSETS:
            oc = asset_ohlc[code].get(d)
            if oc and oc[0] > 0:
                o, c = oc
                row.intraday_pct[code] = (c - o) / o * 100.0
        if row.intraday_pct:  # 최소 한 자산이라도 그날 데이터가 있어야 채택
            rows.append(row)
    return rows


def run(years: float) -> dict:
    rows = build_rows(years)
    gated_n = sum(1 for r in rows if r.gated)
    result = {
        "generated_at": datetime.now(KST).isoformat(),
        "years": years,
        "total_trading_days": len(rows),
        "gated_days": gated_n,
        "open_days": len(rows) - gated_n,
        "gated_pct": round(gated_n / len(rows), 3) if rows else 0.0,
        "assets": {code: _summary(rows, code) for code in ASSETS},
        "recent_gated_days": [
            {
                "date": r.d.isoformat(),
                "inverse_1x_pct": round(r.intraday_pct.get("114800", float("nan")), 3),
                "long_pct": round(r.intraday_pct.get("069500", float("nan")), 3),
            }
            for r in rows[-60:]
            if r.gated
        ],
    }
    return result


def _print(result: dict) -> None:
    print("\n" + "=" * 72)
    print(f"약세장 인버스 ETF 검증 — 최근 {result['years']}년, {result['total_trading_days']}거래일")
    print(
        f"게이트 차단(약세) {result['gated_days']}일 ({result['gated_pct']:.0%}) / "
        f"통과 {result['open_days']}일"
    )
    print("=" * 72)
    print(f"\n{'자산':28} {'그룹':6} {'표본':>5} {'평균%':>8} {'승률':>6} {'CI95':>20} {'p':>6}")
    print("-" * 80)
    for code, s in result["assets"].items():
        for label in ("gated", "open"):
            g = s[label]
            if g.get("n", 0) == 0:
                continue
            ci = g.get("boot_ci95")
            ci_s = f"[{ci[0]:+.3f},{ci[1]:+.3f}]" if ci else ""
            p_s = f"{g['boot_p']:.3f}" if "boot_p" in g else ""
            tag = "차단일" if label == "gated" else "통과일"
            print(
                f"{s['name'][:27]:28} {tag:6} {g['n']:>5} {g['mean_pct']:>+8.3f} "
                f"{g['win_rate']:>6.0%} {ci_s:>20} {p_s:>6}"
            )
        print()


# ── 자체 검증 (네트워크 없이 스크립트 로직 확인) ────────────────────────────
def _self_check() -> None:
    """합성: KOSPI 가 추세 하락 + 인버스가 정확히 반대로 움직이는 세계."""
    rows: list[DayRow] = []
    price = 100.0
    closes: list[tuple[date, float]] = []
    base = date(2025, 1, 1)
    for i in range(120):
        price *= 0.997  # 꾸준한 하락
        closes.append((base + timedelta(days=i), price))
    for i in range(SMA_LONG, len(closes)):
        d, _ = closes[i]
        prior_desc = [c for _, c in reversed(closes[:i])]
        above, *_ = _gate_above(prior_desc)
        r = DayRow(d=d, gated=not above, kospi_close=0, sma5=0, sma20=0)
        # 하락장이므로 long 은 -0.3%, 인버스(-1x)는 +0.3% (시가→종가)
        r.intraday_pct = {"069500": -0.3, "114800": +0.3, "252670": +0.6}
        rows.append(r)
    assert all(r.gated for r in rows), "꾸준한 하락이면 게이트는 전부 차단이어야 함"
    inv = _summary(rows, "114800")
    long_ = _summary(rows, "069500")
    assert inv["gated"]["mean_pct"] > 0, "하락장 인버스 평균은 양수여야 함"
    assert long_["gated"]["mean_pct"] < 0, "하락장 long 평균은 음수여야 함"
    print("✓ self-check 통과: 하락 세계에서 게이트 전부 차단 + 인버스>0>long.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=3.0)
    ap.add_argument("--self-check", action="store_true")
    args = ap.parse_args()

    if args.self_check:
        _self_check()
        return

    random.seed(20260630)
    result = run(args.years)
    _print(result)

    STUDY_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    out = STUDY_DIR / f"inverse_etf_study_{stamp}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n결과 저장: {out}")


if __name__ == "__main__":
    main()
