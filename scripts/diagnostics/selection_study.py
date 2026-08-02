"""선별(selection) 효과 읽기전용 검증 — "거래량만 vs 거래량+추세" 비교.

무엇/왜:
- 현재 day-trade 발굴은 `market.rank.top_volume` 로 '오늘 거래량 상위 N개'를 고른 뒤
  사실상 무조건 매수한다(종목 자신의 추세를 거의 안 본다). 가설: 같은 거래량 풀에서
  '전날까지의 종목 추세'로 재정렬해 같은 K개를 고르면(=거래 횟수 불변, gate 아님
  ranking), 고른 종목의 당일 수익이 더 좋아진다.
- 백테스트 엔진(`backtest/engine.py`)은 일봉·단일종목 스윙만 돌려 교차선별을 못 한다.
  그래서 별도 읽기전용 스크립트로, 단타 진입~청산을 '당일 시가→종가 수익률'로 근사해
  싸게 방향성만 확인한다.

방식:
1. 1회 라이브 `top_volume(top_n≈20)` 로 오늘 거래량 상위 바스켓을 고정(하드코딩 회피).
2. 각 종목 약 3년 일봉을 `get_daily_bars` 로 받는다.
3. 각 거래일 D 마다: **전날(D-1) 거래량**으로 넓은 풀(상위 P) → baseline=풀 상위 K(거래량만),
   improved=풀을 **연속 추세점수**로 재정렬한 상위 K. 추세점수는 D-1 까지의 일봉만 사용.
   성과 = 그 K개의 당일 (close-open)/open 평균.
   ※ 순위에 D일 거래량을 쓰면 '그날 크게 움직인 종목'을 미리 보는 룩어헤드가 되어 baseline 이
     부당하게 유리해진다(거래량↔당일수익 동시상관). 선택 시점에 실제로 아는 D-1 거래량으로 순위.
4. 전 기간 baseline vs improved 평균수익률·승률 비교 + **페어드 차이의 블록 부트스트랩 CI**.

적대적 리뷰(2026-06-07) 반영 — 1차 설계의 치명적 결함 수정:
- (구) 추세점수가 정수 0~4 라 동점이 많고 동점깨기가 거래량(=baseline 키)이라 improved≈baseline →
  처치가 거의 안 걸려 검정력이 0 에 수렴했다. → **연속 점수(풀 내 횡단면 z-점수 합)** 로 교체해
  매일 실제로 재정렬되게 함. `improved≠baseline` 비율을 함께 출력해 처치가 걸렸는지 확인.
- 단일 12개월 → **약 3년**(표본·레짐 ↑, 검정력 ↑).
- i.i.d. 페어드 t 는 겹치는 날의 자기상관을 무시 → **이동블록 부트스트랩**으로 CI·SE 재산출.
  표준편차도 표본(n-1)으로. 검출가능 최소효과(MDE)도 출력.
- RSI 는 (구) '적정 밴드'(상한컷=부호 의심) 대신 **연속 모멘텀**(높을수록 강함)으로 z-점수에 포함.

남은 한계(정직): 시가→종가 근사(장중 진입·지수게이트 무시), 비용 제외, **고정 바스켓=오늘
거래량 상위의 과거 투영(생존편향)**, mock 데이터 의존 → '방향성 신호'일 뿐 증명 아님.

실행: uv run python scripts/diagnostics/selection_study.py
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
from auto_trading.strategy.indicators import rsi_wilder, sma

# ── 스터디 파라미터 (사전 고정: 사후 노브 튜닝 회피) ──────────────────────────
BASKET_SIZE = 20  # 고정 바스켓 크기 (오늘 거래량 상위)
LOOKBACK_DAYS = 365 * 3  # 평가 구간 약 3년 (표본·레짐 ↑ → 검정력 ↑)
POOL_P = 10  # 거래량 넓은 풀 크기 (재정렬 대상)
PICK_K = 3  # 실제 매수 종목 수 (top_n 기본=3 과 일치)
RSI_PERIOD = 14
SMA_FAST, SMA_SLOW = 5, 20
MAX_PAGES = 12  # 일봉 페이지 (3년 + 워밍업 확보; 페이지당 ~600봉)
# 키움 mock 의 ka10081(일봉) 한도가 빡빡해 기본 페이싱(0.15s)으론 429 폭주.
# 다종목 일봉 배치라 선제 간격을 넉넉히 + 재시도 횟수를 늘려 안전하게.
REQUEST_INTERVAL_SEC = 0.8
MAX_RETRIES = 6
# 이동블록 부트스트랩 (페어드 차이의 자기상관 보정)
BOOT_N = 5000
BOOT_BLOCK = 10
BOOT_SEED = 20260607


@dataclass
class DayRow:
    """한 종목의 하루치 평가 입력. 추세는 스칼라 점수가 아니라 원시 특성으로 보관 →
    평가 단계에서 풀 내 횡단면 z-표준화로 연속 점수를 만든다(처치 희석 방지)."""

    open: float
    close: float
    rank_volume: int  # 순위용 = 전날(D-1) 거래량 (룩어헤드 회피: D일 거래량은 마감 후에야 앎)
    # 아래는 모두 D-1 까지의 일봉으로 계산한, 스케일프리(종목 간 비교 가능) 추세 특성.
    f_align: float  # sma5/sma20 - 1  (정배열 강도)
    f_above: float  # prev_close/sma20 - 1  (종가가 추세선 위/아래 강도)
    f_slope: float  # sma5/sma5_prev - 1  (단기선 기울기)
    f_rsi: float  # rsi14 (0~100, 높을수록 모멘텀 강함 — 연속)


_TREND_FEATURES = ("f_align", "f_above", "f_slope", "f_rsi")


def _open_close_return(row: DayRow) -> float:
    """당일 시가→종가 수익률 (단타 진입~청산 근사)."""
    if row.open <= 0:
        return 0.0
    return (row.close - row.open) / row.open


def _trend_features(prior_closes: list[float]) -> tuple[float, float, float, float] | None:
    """D-1 까지의 종가만으로 연속 추세 특성 4개. 미달/0분모면 None.

    (f_align, f_above, f_slope, f_rsi). 모두 스케일프리라 종목 간 비교 가능.
    """
    sma_fast = sma(prior_closes, SMA_FAST)
    sma_slow = sma(prior_closes, SMA_SLOW)
    sma_fast_prev = sma(prior_closes[:-1], SMA_FAST)
    rsi = rsi_wilder(prior_closes, RSI_PERIOD)
    if sma_fast is None or sma_slow is None or sma_fast_prev is None or rsi is None:
        return None
    if sma_slow <= 0 or sma_fast_prev <= 0:
        return None
    prev_close = prior_closes[-1]
    f_align = sma_fast / sma_slow - 1.0
    f_above = prev_close / sma_slow - 1.0
    f_slope = sma_fast / sma_fast_prev - 1.0
    return f_align, f_above, f_slope, rsi


def _build_panel(
    bars_by_symbol: dict[str, list[Bar]],
    *,
    cutoff: date,
) -> dict[date, dict[str, DayRow]]:
    """종목별 일봉 → {날짜: {종목: DayRow}}. 평가일은 cutoff 이후만, 추세는 그 전 봉으로."""
    panel: dict[date, dict[str, DayRow]] = {}
    for symbol, bars in bars_by_symbol.items():
        ordered = sorted(bars, key=lambda b: b.ts)
        closes: list[float] = []
        prev_volume: int | None = None  # 전날 거래량 — 순위용(룩어헤드 회피)
        for bar in ordered:
            d = bar.ts.date()
            # prev_volume is None 인 첫 봉은 순위 재료가 없어 제외.
            if d >= cutoff and bar.open > 0 and prev_volume is not None:
                feats = _trend_features(closes)  # closes = D-1 까지 (현재 봉 미포함)
                if feats is not None:
                    panel.setdefault(d, {})[symbol] = DayRow(
                        open=bar.open,
                        close=bar.close,
                        rank_volume=prev_volume,
                        f_align=feats[0],
                        f_above=feats[1],
                        f_slope=feats[2],
                        f_rsi=feats[3],
                    )
            closes.append(bar.close)
            prev_volume = bar.volume
    return panel


def _composite_scores(pool: list[tuple[str, DayRow]]) -> dict[str, float]:
    """풀 내 횡단면 z-표준화 후 4개 특성의 z-점수 합 = 연속 추세점수.

    각 특성을 풀(=그날 후보 P종목) 기준으로 (x-평균)/표준편차 표준화 → 합산.
    스케일이 다른 특성(비율 vs RSI)을 동등 비교하게 하고, 연속값이라 매일 실제로
    순위가 바뀐다(정수 점수의 동점 희석 문제 제거).
    """
    scores: dict[str, float] = {sym: 0.0 for sym, _ in pool}
    for feat in _TREND_FEATURES:
        vals = [getattr(r, feat) for _, r in pool]
        mean = statistics.fmean(vals)
        sd = statistics.pstdev(vals) if len(vals) > 1 else 0.0
        if sd <= 0:
            continue  # 전부 동일 → 변별 없음, 0 기여
        for sym, r in pool:
            scores[sym] += (getattr(r, feat) - mean) / sd
    return scores


@dataclass
class Aggregate:
    """한 전략(baseline/improved/pool)의 집계."""

    daily_returns: list[float]

    @property
    def n_days(self) -> int:
        return len(self.daily_returns)

    @property
    def mean_pct(self) -> float:
        return 100.0 * statistics.fmean(self.daily_returns) if self.daily_returns else 0.0

    @property
    def std_pct(self) -> float:
        # 표본 표준편차(n-1) — 모표준편차(pstdev)는 t 를 미세 과대평가.
        return 100.0 * statistics.stdev(self.daily_returns) if len(self.daily_returns) > 1 else 0.0

    @property
    def win_rate_pct(self) -> float:
        if not self.daily_returns:
            return 0.0
        wins = sum(1 for r in self.daily_returns if r > 0)
        return 100.0 * wins / len(self.daily_returns)


def _block_bootstrap(diffs: list[float]) -> dict[str, float]:
    """이동블록 부트스트랩 — 페어드 차이의 자기상관을 견디는 SE·95%CI·p 근사.

    겹치는 날들의 수익률이 상관돼 i.i.d. 페어드 t 가 과신될 수 있어, 길이 BOOT_BLOCK
    블록을 순환 재표집해 평균 분포를 만든다.
    """
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
    # 양측 p 근사: 부트스트랩 평균이 0 을 가로지르는 비율 x2.
    if obs >= 0:
        p = 2.0 * sum(1 for m in means if m <= 0) / BOOT_N
    else:
        p = 2.0 * sum(1 for m in means if m >= 0) / BOOT_N
    return {"se": se, "lo": lo, "hi": hi, "p": min(1.0, p)}


def _evaluate(panel: dict[date, dict[str, DayRow]]) -> dict[str, object]:
    """각 거래일에 baseline/improved/pool 선별 후 당일 수익 집계 + 페어드 비교."""
    base_rets: list[float] = []
    impr_rets: list[float] = []
    pool_rets: list[float] = []
    paired_diffs: list[float] = []  # improved - baseline (같은 날)
    eval_days = 0
    differ_days = 0  # improved 종목집합이 baseline 과 실제로 달랐던 날 (처치가 걸린 날)

    for d in sorted(panel):
        rows = panel[d]
        if len(rows) < PICK_K:
            continue
        # 넓은 풀: 전날 거래량 상위 P (룩어헤드 회피)
        by_volume = sorted(rows.items(), key=lambda kv: kv[1].rank_volume, reverse=True)
        pool = by_volume[:POOL_P]
        if len(pool) < PICK_K:
            continue
        eval_days += 1

        baseline = pool[:PICK_K]
        # improved: 같은 풀을 연속 추세점수(z합) desc 로 재정렬 (동점시 거래량)
        scores = _composite_scores(pool)
        improved = sorted(pool, key=lambda kv: (scores[kv[0]], kv[1].rank_volume), reverse=True)[
            :PICK_K
        ]

        if {s for s, _ in improved} != {s for s, _ in baseline}:
            differ_days += 1

        base_r = statistics.fmean(_open_close_return(r) for _, r in baseline)
        impr_r = statistics.fmean(_open_close_return(r) for _, r in improved)
        pool_r = statistics.fmean(_open_close_return(r) for _, r in pool)

        base_rets.append(base_r)
        impr_rets.append(impr_r)
        pool_rets.append(pool_r)
        paired_diffs.append(impr_r - base_r)

    # i.i.d. 페어드 t (표본 sd) — 참고용. 주된 추론은 블록 부트스트랩 CI.
    paired_mean = statistics.fmean(paired_diffs) if paired_diffs else 0.0
    paired_sd = statistics.stdev(paired_diffs) if len(paired_diffs) > 1 else 0.0
    n = len(paired_diffs)
    se_iid = (paired_sd / math.sqrt(n)) if n > 0 and paired_sd > 0 else 0.0
    paired_t = (paired_mean / se_iid) if se_iid > 0 else 0.0
    mde = 2.0 * se_iid  # |t|=2 에서 검출가능 최소효과
    boot = _block_bootstrap(paired_diffs)

    return {
        "eval_days": eval_days,
        "differ_days": differ_days,
        "baseline": Aggregate(base_rets),
        "improved": Aggregate(impr_rets),
        "pool": Aggregate(pool_rets),
        "paired_mean_diff_pct": 100.0 * paired_mean,
        "paired_t": paired_t,
        "mde_pct": 100.0 * mde,
        "boot_se_pct": 100.0 * boot["se"],
        "boot_lo_pct": 100.0 * boot["lo"],
        "boot_hi_pct": 100.0 * boot["hi"],
        "boot_p": boot["p"],
    }


async def _fetch_all() -> tuple[list[RankedSymbol], dict[str, list[Bar]]]:
    """바스켓 1회 조회 + 각 종목 일봉 조회 (순차, 레이트리밋 보수적).

    종목별 fetch 는 try/except 로 감싸 한 종목 실패가 전체를 죽이지 않게 하고,
    진행상황을 한 줄씩 출력해 사용자가 멈춤/진척을 구분할 수 있게 한다.
    """
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


def _verdict(result: dict[str, object]) -> str:
    """부트스트랩 95%CI 기준 판정 — i.i.d. t 가 아니라 CI 가 0 을 포함하는지로."""
    lo = result["boot_lo_pct"]
    hi = result["boot_hi_pct"]
    if lo > 0:
        return "improved 유의 우위 (부트스트랩 95%CI 가 0 위)"
    if hi < 0:
        return "improved 유의 열위 (부트스트랩 95%CI 가 0 아래)"
    return "불확실 — CI 가 0 을 포함 (효과 있음/없음 단정 불가)"


def _print_report(
    basket: list[RankedSymbol],
    result: dict[str, object],
    cutoff: date,
) -> None:
    base: Aggregate = result["baseline"]  # type: ignore[assignment]
    impr: Aggregate = result["improved"]  # type: ignore[assignment]
    pool: Aggregate = result["pool"]  # type: ignore[assignment]
    eval_days = result["eval_days"]
    differ = result["differ_days"]
    differ_pct = (100.0 * differ / eval_days) if eval_days else 0.0  # type: ignore[operator]

    print("\n" + "=" * 70)
    print("선별 검증: 거래량만(baseline) vs 거래량+연속추세(improved)")
    print("=" * 70)
    print(f"바스켓: {len(basket)}종목 | 평가구간: {cutoff} ~ 오늘 | 평가일수: {eval_days}")
    print(
        f"풀 P={POOL_P}, 매수 K={PICK_K}, SMA {SMA_FAST}/{SMA_SLOW}, RSI{RSI_PERIOD} | "
        f"처치 적용일(improved≠baseline): {differ}일 ({differ_pct:.0f}%)"
    )
    print("-" * 70)
    print(f"{'전략':<24}{'평균일수익%':>12}{'승률%':>10}{'표준편차%':>12}")
    print(
        f"{'baseline(거래량만)':<24}{base.mean_pct:>12.4f}{base.win_rate_pct:>10.1f}{base.std_pct:>12.4f}"
    )
    print(
        f"{'improved(거래량+추세)':<24}{impr.mean_pct:>12.4f}{impr.win_rate_pct:>10.1f}{impr.std_pct:>12.4f}"
    )
    print(
        f"{'pool(풀 전체 평균)':<24}{pool.mean_pct:>12.4f}{pool.win_rate_pct:>10.1f}{pool.std_pct:>12.4f}"
    )
    print("-" * 70)
    diff = result["paired_mean_diff_pct"]
    t = result["paired_t"]
    mde = result["mde_pct"]
    print(f"페어드 평균차(improved-baseline): {diff:>+.4f}%/일")
    print(f"  i.i.d. 참고: t≈{t:>+.2f}, 검출가능 최소효과(|t|=2)≈{mde:.4f}%/일")
    print(
        f"  블록부트스트랩: SE={result['boot_se_pct']:.4f}%  "
        f"95%CI[{result['boot_lo_pct']:+.4f}, {result['boot_hi_pct']:+.4f}]%/일  p≈{result['boot_p']:.3f}"
    )
    print(f"판정: {_verdict(result)}")
    print("=" * 70)
    print(
        "⚠️ 한계: 시가→종가 근사(장중 진입·지수게이트 무시)·비용 제외·"
        "고정 바스켓=오늘 상위의 과거투영(생존편향)·mock 데이터 의존."
    )
    print("   → 방향성 신호일 뿐 증명 아님. CI 가 0 포함이면 결론 보류가 정직.\n")


def main() -> None:
    cutoff = (datetime.now(KST).date()) - timedelta(days=LOOKBACK_DAYS)
    yrs = LOOKBACK_DAYS / 365
    print(
        f"바스켓 {BASKET_SIZE}종목 x 약 {yrs:.0f}년 일봉을 받는 중… "
        f"(요청 간격 {REQUEST_INTERVAL_SEC}s, 종목당 최대 {MAX_PAGES}p — 1~2분 소요 가능)"
    )
    basket, bars_by_symbol = asyncio.run(_fetch_all())
    if not bars_by_symbol:
        print("[오류] 일봉 데이터를 못 받았습니다. KIWOOM_MODE/.env/네트워크를 확인하세요.")
        return

    panel = _build_panel(bars_by_symbol, cutoff=cutoff)
    result = _evaluate(panel)
    _print_report(basket, result, cutoff)

    # JSON 산출물 (data/studies/)
    base: Aggregate = result["baseline"]  # type: ignore[assignment]
    impr: Aggregate = result["improved"]  # type: ignore[assignment]
    pool: Aggregate = result["pool"]  # type: ignore[assignment]
    out_dir = Path("data/studies")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"selection_study_{stamp}.json"
    payload = {
        "generated_at": datetime.now(KST).isoformat(),
        "params": {
            "basket_size": len(basket),
            "lookback_days": LOOKBACK_DAYS,
            "pool_p": POOL_P,
            "pick_k": PICK_K,
            "rsi_period": RSI_PERIOD,
            "sma": [SMA_FAST, SMA_SLOW],
            "trend_score": "continuous z-sum of (sma5/sma20-1, close/sma20-1, sma5/sma5_prev-1, rsi)",
            "bootstrap": {"n": BOOT_N, "block": BOOT_BLOCK},
        },
        "basket": [
            {"symbol": rs.symbol, "name": rs.name, "market": rs.market, "volume": rs.volume}
            for rs in basket
        ],
        "eval_days": result["eval_days"],
        "differ_days": result["differ_days"],
        "results": {
            "baseline": {
                "mean_pct": base.mean_pct,
                "win_rate_pct": base.win_rate_pct,
                "std_pct": base.std_pct,
            },
            "improved": {
                "mean_pct": impr.mean_pct,
                "win_rate_pct": impr.win_rate_pct,
                "std_pct": impr.std_pct,
            },
            "pool": {
                "mean_pct": pool.mean_pct,
                "win_rate_pct": pool.win_rate_pct,
                "std_pct": pool.std_pct,
            },
        },
        "paired_mean_diff_pct": result["paired_mean_diff_pct"],
        "paired_t_iid": result["paired_t"],
        "mde_pct_iid": result["mde_pct"],
        "boot_se_pct": result["boot_se_pct"],
        "boot_ci95_pct": [result["boot_lo_pct"], result["boot_hi_pct"]],
        "boot_p": result["boot_p"],
        "verdict": _verdict(result),
        "caveats": "open->close approx, no costs, fixed survivorship basket, mock data; directional only",
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"📄 결과 저장: {out_path}")


if __name__ == "__main__":
    main()
