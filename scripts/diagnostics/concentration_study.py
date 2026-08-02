"""집중도/분산(concentration) 효과 읽기전용 검증 — "거래량 상위 몇 개를 살까".

무엇/왜:
- `selection_study.py` 결과(3년·728일) 셋이 일관되게 **pool(상위10 평균) > baseline(상위3)**.
  즉 추세가 아니라 "거래량 최상위에 덜 집중할수록 덜 나쁘다"가 진짜 신호일 수 있다.
- 이 스크립트는 그 가설을 분해해 검증한다:
  (1) 집중도: 매수 수 K ∈ {1,2,3,5,10} 별 당일 시가→종가 수익의 평균·승률·위험조정(평균/표준편차).
      단순 분산은 표준편차가 주는 게 당연하므로 **평균**과 **평균/표준편차**를 핵심으로 본다.
  (2) 음의 선택: 거래량 1~3등(top3) vs 4~10등(mid)의 평균 비교. mid > top3 이면 "최상위 거래량
      종목이 특히 나쁘다" → 싸게 실행가능(YAML top_n 조정/최상위 회피).
- 추론은 이동블록 부트스트랩 95%CI (겹치는 날 자기상관 보정). 페어드 비교는 같은 날 동일 풀에서
  슬라이스만 달리하므로 바스켓/레짐 교란이 상쇄된다(상대 비교가 강건).

방식: `selection_study.py` 와 동일하게 1회 `top_volume` 바스켓 + 약 3년 일봉. 순위는 **전날(D-1)
거래량**(룩어헤드 회피). 추세지표는 불필요하므로 워밍업 요건이 없어 평가일수가 더 많다.

한계(정직): 시가→종가 근사·비용 제외(K↑=포지션↑=실거래 수수료↑, 미반영)·고정 바스켓
생존편향(절대수치 신뢰말 것, 상대순위만)·mock 의존 → 방향성 신호일 뿐.

실행: uv run python scripts/diagnostics/concentration_study.py
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from auto_trading._common import KST, Bar
from auto_trading.kiwoom.rest import KiwoomRestClient
from auto_trading.market.chart import get_daily_bars
from auto_trading.market.rank import RankedSymbol, top_volume

# ── 파라미터 (사전 고정) ──────────────────────────────────────────────────────
BASKET_SIZE = 20
LOOKBACK_DAYS = 365 * 3
POOL_P = 10  # 거래량 상위 풀
K_LEVELS = (1, 2, 3, 5, 10)  # 매수 종목 수 비교
TOP_SLICE = 3  # 음의선택 비교: 1~3등
MID_SLICE = (3, 10)  # vs 4~10등 (인덱스 3..9)
MAX_PAGES = 12
REQUEST_INTERVAL_SEC = 0.8
MAX_RETRIES = 6
BOOT_N = 5000
BOOT_BLOCK = 10
BOOT_SEED = 20260607


@dataclass
class DayRow:
    open: float
    close: float
    rank_volume: int  # 전날(D-1) 거래량 — 순위용(룩어헤드 회피)


def _ret(row: DayRow) -> float:
    if row.open <= 0:
        return 0.0
    return (row.close - row.open) / row.open


def _build_panel(
    bars_by_symbol: dict[str, list[Bar]], *, cutoff: date
) -> dict[date, dict[str, DayRow]]:
    panel: dict[date, dict[str, DayRow]] = {}
    for symbol, bars in bars_by_symbol.items():
        ordered = sorted(bars, key=lambda b: b.ts)
        prev_volume: int | None = None
        for bar in ordered:
            d = bar.ts.date()
            if d >= cutoff and bar.open > 0 and prev_volume is not None:
                panel.setdefault(d, {})[symbol] = DayRow(
                    open=bar.open, close=bar.close, rank_volume=prev_volume
                )
            prev_volume = bar.volume
    return panel


def _agg(returns: list[float]) -> dict[str, float]:
    if not returns:
        return {"mean_pct": 0.0, "std_pct": 0.0, "win_pct": 0.0, "sharpe": 0.0, "n": 0}
    mean = statistics.fmean(returns)
    std = statistics.stdev(returns) if len(returns) > 1 else 0.0
    wins = sum(1 for r in returns if r > 0)
    return {
        "mean_pct": 100.0 * mean,
        "std_pct": 100.0 * std,
        "win_pct": 100.0 * wins / len(returns),
        "sharpe": (mean / std) if std > 0 else 0.0,  # 일별 위험조정(평균/표준편차)
        "n": len(returns),
    }


def _block_bootstrap(diffs: list[float]) -> dict[str, float]:
    """이동블록 부트스트랩 — 페어드 차이의 SE·95%CI·양측 p 근사."""
    n = len(diffs)
    if n < 2:
        return {"se": 0.0, "lo": 0.0, "hi": 0.0, "p": 1.0}
    rng = random.Random(BOOT_SEED)
    n_blocks = math.ceil(n / BOOT_BLOCK)
    means: list[float] = []
    for _ in range(BOOT_N):
        sample: list[float] = []
        for _ in range(n_blocks):
            start = rng.randrange(n)
            for j in range(BOOT_BLOCK):
                sample.append(diffs[(start + j) % n])
        means.append(statistics.fmean(sample[:n]))
    means.sort()
    lo = means[int(0.025 * BOOT_N)]
    hi = means[int(0.975 * BOOT_N)]
    se = statistics.stdev(means)
    obs = statistics.fmean(diffs)
    if obs >= 0:
        p = 2.0 * sum(1 for m in means if m <= 0) / BOOT_N
    else:
        p = 2.0 * sum(1 for m in means if m >= 0) / BOOT_N
    return {"se": 100.0 * se, "lo": 100.0 * lo, "hi": 100.0 * hi, "p": min(1.0, p)}


def _evaluate(panel: dict[date, dict[str, DayRow]]) -> dict[str, object]:
    """같은 날·같은 풀에서 K별/슬라이스별 당일수익을 모아 페어드 비교."""
    k_rets: dict[int, list[float]] = {k: [] for k in K_LEVELS}
    top_rets: list[float] = []  # 1~3등
    mid_rets: list[float] = []  # 4~10등
    diff_k10_k3: list[float] = []  # K=10 - K=3 (같은 날)
    diff_mid_top: list[float] = []  # mid - top3 (같은 날)
    eval_days = 0

    for d in sorted(panel):
        rows = panel[d]
        # 모든 K(최대 10)를 같은 날 정의하려면 풀이 POOL_P 이상이어야 공정.
        if len(rows) < POOL_P:
            continue
        pool = sorted(rows.values(), key=lambda r: r.rank_volume, reverse=True)[:POOL_P]
        eval_days += 1

        rets = [_ret(r) for r in pool]  # 풀 내 거래량 desc 순서
        for k in K_LEVELS:
            k_rets[k].append(statistics.fmean(rets[:k]))

        top = statistics.fmean(rets[:TOP_SLICE])
        mid = statistics.fmean(rets[MID_SLICE[0] : MID_SLICE[1]])
        top_rets.append(top)
        mid_rets.append(mid)
        diff_k10_k3.append(statistics.fmean(rets[:10]) - statistics.fmean(rets[:3]))
        diff_mid_top.append(mid - top)

    return {
        "eval_days": eval_days,
        "by_k": {k: _agg(k_rets[k]) for k in K_LEVELS},
        "top3": _agg(top_rets),
        "mid4_10": _agg(mid_rets),
        "diff_k10_k3_pct": 100.0 * statistics.fmean(diff_k10_k3) if diff_k10_k3 else 0.0,
        "boot_k10_k3": _block_bootstrap(diff_k10_k3),
        "diff_mid_top_pct": 100.0 * statistics.fmean(diff_mid_top) if diff_mid_top else 0.0,
        "boot_mid_top": _block_bootstrap(diff_mid_top),
    }


async def _fetch_all() -> tuple[list[RankedSymbol], dict[str, list[Bar]]]:
    async with KiwoomRestClient(
        min_request_interval_sec=REQUEST_INTERVAL_SEC, max_retries=MAX_RETRIES
    ) as client:
        basket = await top_volume(client, top_n=BASKET_SIZE, exclude_etf=True)
        bars_by_symbol: dict[str, list[Bar]] = {}
        total = len(basket)
        for i, rs in enumerate(basket, 1):
            try:
                bars = await get_daily_bars(client, rs.symbol, max_pages=MAX_PAGES)
            except Exception as exc:
                print(f"  [{i}/{total}] {rs.symbol} {rs.name}: 실패({type(exc).__name__}) → 건너뜀")
                continue
            if bars:
                bars_by_symbol[rs.symbol] = bars
                print(f"  [{i}/{total}] {rs.symbol} {rs.name}: 일봉 {len(bars)}개")
            else:
                print(f"  [{i}/{total}] {rs.symbol} {rs.name}: 빈 응답 → 건너뜀")
        return basket, bars_by_symbol


def _verdict(boot: dict[str, float], *, positive_is_good: str, negative_is_good: str) -> str:
    if boot["lo"] > 0:
        return positive_is_good
    if boot["hi"] < 0:
        return negative_is_good
    return "불확실 — CI 가 0 포함"


def _print_report(basket: list[RankedSymbol], result: dict[str, object], cutoff: date) -> None:
    by_k: dict[int, dict[str, float]] = result["by_k"]  # type: ignore[assignment]
    print("\n" + "=" * 70)
    print("집중도/분산 검증: 거래량 상위 K개 매수 (당일 시가→종가)")
    print("=" * 70)
    print(
        f"바스켓: {len(basket)}종목 | 평가구간: {cutoff} ~ 오늘 | 평가일수: {result['eval_days']}"
    )
    print("-" * 70)
    print(
        f"{'K(매수수)':<10}{'평균일수익%':>12}{'승률%':>9}{'표준편차%':>11}{'위험조정(평균/sd)':>16}"
    )
    for k in K_LEVELS:
        a = by_k[k]
        print(
            f"{k:<10}{a['mean_pct']:>12.4f}{a['win_pct']:>9.1f}{a['std_pct']:>11.4f}{a['sharpe']:>16.4f}"
        )
    print("-" * 70)
    top: dict[str, float] = result["top3"]  # type: ignore[assignment]
    mid: dict[str, float] = result["mid4_10"]  # type: ignore[assignment]
    print(
        f"음의선택  1~3등 평균={top['mean_pct']:+.4f}%/일(승률{top['win_pct']:.1f})  "
        f"4~10등 평균={mid['mean_pct']:+.4f}%/일(승률{mid['win_pct']:.1f})"
    )
    print("-" * 70)
    bk = result["boot_k10_k3"]  # type: ignore[assignment]
    bm = result["boot_mid_top"]  # type: ignore[assignment]
    print(
        f"분산효과 (K=10 - K=3): {result['diff_k10_k3_pct']:+.4f}%/일  "
        f"95%CI[{bk['lo']:+.4f},{bk['hi']:+.4f}] p≈{bk['p']:.3f}  → "
        f"{_verdict(bk, positive_is_good='분산 우위(많이 살수록 평균↑)', negative_is_good='집중 우위')}"
    )
    print(
        f"음의선택 (4~10등 - 1~3등): {result['diff_mid_top_pct']:+.4f}%/일  "
        f"95%CI[{bm['lo']:+.4f},{bm['hi']:+.4f}] p≈{bm['p']:.3f}  → "
        f"{_verdict(bm, positive_is_good='최상위 거래량 회피 유효(4~10등이 나음)', negative_is_good='최상위가 나음')}"
    )
    print("=" * 70)
    print("⚠️ 한계: 시가→종가 근사·비용 제외(K↑=수수료↑ 미반영)·고정 바스켓 생존편향·mock 의존.")
    print("   위험조정·평균이 핵심(표준편차 감소는 분산의 당연한 산술효과). CI 0포함이면 보류.\n")


def main() -> None:
    cutoff = (datetime.now(KST).date()) - timedelta(days=LOOKBACK_DAYS)
    print(
        f"바스켓 {BASKET_SIZE}종목 x 약 {LOOKBACK_DAYS / 365:.0f}년 일봉을 받는 중… "
        f"(요청 간격 {REQUEST_INTERVAL_SEC}s — 1~2분 소요 가능)"
    )
    basket, bars_by_symbol = asyncio.run(_fetch_all())
    if not bars_by_symbol:
        print("[오류] 일봉 데이터를 못 받았습니다. KIWOOM_MODE/.env/네트워크를 확인하세요.")
        return

    panel = _build_panel(bars_by_symbol, cutoff=cutoff)
    result = _evaluate(panel)
    _print_report(basket, result, cutoff)

    out_dir = Path("data/studies")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"concentration_study_{stamp}.json"
    payload = {
        "generated_at": datetime.now(KST).isoformat(),
        "params": {
            "basket_size": len(basket),
            "lookback_days": LOOKBACK_DAYS,
            "pool_p": POOL_P,
            "k_levels": list(K_LEVELS),
            "bootstrap": {"n": BOOT_N, "block": BOOT_BLOCK},
        },
        "basket": [
            {"symbol": rs.symbol, "name": rs.name, "market": rs.market, "volume": rs.volume}
            for rs in basket
        ],
        "eval_days": result["eval_days"],
        "by_k": result["by_k"],
        "top3": result["top3"],
        "mid4_10": result["mid4_10"],
        "diff_k10_k3_pct": result["diff_k10_k3_pct"],
        "boot_k10_k3": result["boot_k10_k3"],
        "diff_mid_top_pct": result["diff_mid_top_pct"],
        "boot_mid_top": result["boot_mid_top"],
        "caveats": "open->close approx, no costs (more K=more commission unmodeled), fixed survivorship basket, mock data; directional only",
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"📄 결과 저장: {out_path}")


if __name__ == "__main__":
    main()
