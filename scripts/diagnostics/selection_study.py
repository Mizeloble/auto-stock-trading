"""선별(selection) 효과 읽기전용 검증 — "거래량만 vs 거래량+추세" 비교.

무엇/왜:
- 현재 day-trade 발굴은 `market.rank.top_volume` 로 '오늘 거래량 상위 N개'를 고른 뒤
  사실상 무조건 매수한다(종목 자신의 추세를 거의 안 본다). 가설: 같은 거래량 풀에서
  '전날까지의 종목 추세 점수'로 재정렬해 같은 K개를 고르면(=거래 횟수 불변, gate 아님
  ranking), 고른 종목의 당일 수익이 더 좋아진다.
- 백테스트 엔진(`backtest/engine.py`)은 일봉·단일종목 스윙만 돌려 교차선별을 못 한다.
  그래서 별도 읽기전용 스크립트로, 단타 진입~청산을 '당일 시가→종가 수익률'로 근사해
  싸게 방향성만 확인한다.

방식:
1. 1회 라이브 `top_volume(top_n≈20)` 로 오늘 거래량 상위 바스켓을 고정(하드코딩 회피).
2. 각 종목 12개월 일봉을 `get_daily_bars` 로 받는다.
3. 각 거래일 D 마다: 거래량으로 넓은 풀(상위 P) → baseline=풀 상위 K(거래량만),
   improved=풀을 (추세점수, 거래량)로 재정렬한 상위 K. 추세점수는 D-1 까지의 일봉만 사용
   (sma5>sma20·종가 위·5선 상승·RSI 적정). 성과 = 그 K개의 당일 (close-open)/open 평균.
4. 전 기간 baseline vs improved 평균수익률·승률·표준편차·페어드 t 비교 → 표 + JSON.

한계(정직): 시가→종가 근사는 장중 진입타이밍 무시(그건 별도 진입 검증), 수수료·슬리피지
제외, 고정 바스켓≠매일 실제 top-volume, 생존편향, 표본 작음 → '방향성 신호'일 뿐 증명 아님.

실행: uv run python scripts/diagnostics/selection_study.py
"""

from __future__ import annotations

import asyncio
import json
import math
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
LOOKBACK_DAYS = 365  # 평가 구간 12개월
POOL_P = 10  # 거래량 넓은 풀 크기 (재정렬 대상)
PICK_K = 3  # 실제 매수 종목 수 (top_n 기본=3 과 일치)
RSI_PERIOD = 14
RSI_LO, RSI_HI = 45.0, 75.0  # '적정' RSI 밴드 (과매도-사망/과열 양끝 회피)
SMA_FAST, SMA_SLOW = 5, 20
MAX_PAGES = 12  # 일봉 페이지 (12개월 + 워밍업 확보)


@dataclass
class DayRow:
    """한 종목의 하루치 평가 입력."""

    open: float
    close: float
    volume: int
    trend_score: int  # 0..4, D-1 까지 일봉으로 계산


def _open_close_return(row: DayRow) -> float:
    """당일 시가→종가 수익률 (단타 진입~청산 근사)."""
    if row.open <= 0:
        return 0.0
    return (row.close - row.open) / row.open


def _trend_score(prior_closes: list[float]) -> int | None:
    """D-1 까지의 종가만으로 추세 점수(0..4). 미달이면 None.

    구성요소(각 1점):
    - 정배열: sma5 > sma20
    - 종가 위: 직전 종가 > sma5 그리고 > sma20
    - 5선 상승: sma5(D-1) > sma5(D-2)
    - RSI 적정: RSI_LO <= rsi14 <= RSI_HI
    """
    sma_fast = sma(prior_closes, SMA_FAST)
    sma_slow = sma(prior_closes, SMA_SLOW)
    sma_fast_prev = sma(prior_closes[:-1], SMA_FAST)
    rsi = rsi_wilder(prior_closes, RSI_PERIOD)
    if sma_fast is None or sma_slow is None or sma_fast_prev is None or rsi is None:
        return None
    prev_close = prior_closes[-1]
    score = 0
    if sma_fast > sma_slow:
        score += 1
    if prev_close > sma_fast and prev_close > sma_slow:
        score += 1
    if sma_fast > sma_fast_prev:
        score += 1
    if RSI_LO <= rsi <= RSI_HI:
        score += 1
    return score


def _build_panel(
    bars_by_symbol: dict[str, list[Bar]],
    *,
    cutoff: date,
) -> dict[date, dict[str, DayRow]]:
    """종목별 일봉 → {날짜: {종목: DayRow}}. 평가일은 cutoff 이후만, 추세점수는 그 전 봉으로."""
    panel: dict[date, dict[str, DayRow]] = {}
    for symbol, bars in bars_by_symbol.items():
        ordered = sorted(bars, key=lambda b: b.ts)
        closes: list[float] = []
        for bar in ordered:
            d = bar.ts.date()
            if d >= cutoff and bar.open > 0:
                score = _trend_score(closes)  # closes = D-1 까지 (현재 봉 미포함)
                if score is not None:
                    panel.setdefault(d, {})[symbol] = DayRow(
                        open=bar.open,
                        close=bar.close,
                        volume=bar.volume,
                        trend_score=score,
                    )
            closes.append(bar.close)
    return panel


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
        return 100.0 * statistics.pstdev(self.daily_returns) if len(self.daily_returns) > 1 else 0.0

    @property
    def win_rate_pct(self) -> float:
        if not self.daily_returns:
            return 0.0
        wins = sum(1 for r in self.daily_returns if r > 0)
        return 100.0 * wins / len(self.daily_returns)


def _evaluate(panel: dict[date, dict[str, DayRow]]) -> dict[str, object]:
    """각 거래일에 baseline/improved/pool 선별 후 당일 수익 집계 + 페어드 비교."""
    base_rets: list[float] = []
    impr_rets: list[float] = []
    pool_rets: list[float] = []
    paired_diffs: list[float] = []  # improved - baseline (같은 날)
    eval_days = 0

    for d in sorted(panel):
        rows = panel[d]
        if len(rows) < PICK_K:
            continue
        # 넓은 풀: 거래량 상위 P
        by_volume = sorted(rows.items(), key=lambda kv: kv[1].volume, reverse=True)
        pool = by_volume[:POOL_P]
        if len(pool) < PICK_K:
            continue
        eval_days += 1

        baseline = pool[:PICK_K]
        # improved: 같은 풀을 (추세점수 desc, 거래량 desc)로 재정렬
        improved = sorted(
            pool, key=lambda kv: (kv[1].trend_score, kv[1].volume), reverse=True
        )[:PICK_K]

        base_r = statistics.fmean(_open_close_return(r) for _, r in baseline)
        impr_r = statistics.fmean(_open_close_return(r) for _, r in improved)
        pool_r = statistics.fmean(_open_close_return(r) for _, r in pool)

        base_rets.append(base_r)
        impr_rets.append(impr_r)
        pool_rets.append(pool_r)
        paired_diffs.append(impr_r - base_r)

    baseline_agg = Aggregate(base_rets)
    improved_agg = Aggregate(impr_rets)
    pool_agg = Aggregate(pool_rets)

    # 페어드 차이의 t 통계 (mean / (sd/sqrt(n))) — 0 대비 유의성 가늠
    paired_t = 0.0
    if len(paired_diffs) > 1:
        sd = statistics.pstdev(paired_diffs)
        if sd > 0:
            paired_t = statistics.fmean(paired_diffs) / (sd / math.sqrt(len(paired_diffs)))

    return {
        "eval_days": eval_days,
        "baseline": baseline_agg,
        "improved": improved_agg,
        "pool": pool_agg,
        "paired_mean_diff_pct": 100.0 * statistics.fmean(paired_diffs) if paired_diffs else 0.0,
        "paired_t": paired_t,
    }


async def _fetch_all() -> tuple[list[RankedSymbol], dict[str, list[Bar]]]:
    """바스켓 1회 조회 + 각 종목 일봉 조회 (순차, 레이트리밋 보수적)."""
    async with KiwoomRestClient() as client:
        basket = await top_volume(client, top_n=BASKET_SIZE, exclude_etf=True)
        bars_by_symbol: dict[str, list[Bar]] = {}
        for rs in basket:
            bars = await get_daily_bars(client, rs.symbol, max_pages=MAX_PAGES)
            if bars:
                bars_by_symbol[rs.symbol] = bars
        return basket, bars_by_symbol


def _print_report(
    basket: list[RankedSymbol],
    result: dict[str, object],
    cutoff: date,
) -> None:
    base: Aggregate = result["baseline"]  # type: ignore[assignment]
    impr: Aggregate = result["improved"]  # type: ignore[assignment]
    pool: Aggregate = result["pool"]  # type: ignore[assignment]

    print("\n" + "=" * 64)
    print("선별 검증: 거래량만(baseline) vs 거래량+추세(improved)")
    print("=" * 64)
    print(f"바스켓: {len(basket)}종목 | 평가구간: {cutoff} ~ 오늘 | 평가일수: {result['eval_days']}")
    print(f"풀 P={POOL_P}, 매수 K={PICK_K}, RSI밴드 [{RSI_LO},{RSI_HI}], SMA {SMA_FAST}/{SMA_SLOW}")
    print("-" * 64)
    print(f"{'전략':<22}{'평균일수익%':>12}{'승률%':>10}{'표준편차%':>12}")
    print(f"{'baseline(거래량만)':<22}{base.mean_pct:>12.4f}{base.win_rate_pct:>10.1f}{base.std_pct:>12.4f}")
    print(f"{'improved(거래량+추세)':<22}{impr.mean_pct:>12.4f}{impr.win_rate_pct:>10.1f}{impr.std_pct:>12.4f}")
    print(f"{'pool(풀 전체 평균)':<22}{pool.mean_pct:>12.4f}{pool.win_rate_pct:>10.1f}{pool.std_pct:>12.4f}")
    print("-" * 64)
    diff = result["paired_mean_diff_pct"]
    t = result["paired_t"]
    print(f"페어드 평균차(improved-baseline): {diff:>+.4f}%/일   t≈{t:>+.2f}")
    verdict = (
        "improved 우위 (t≈|2|↑면 주목)" if diff > 0 else "개선 없음/악화 → 구현 보류 근거"
    )
    print(f"판정 힌트: {verdict}")
    print("=" * 64)
    print("⚠️ 한계: 시가→종가 근사(장중 진입 무시)·비용 제외·고정 바스켓·생존편향·소표본.")
    print("   → 방향성 신호일 뿐 증명 아님. |t|<2 면 결론 보류가 정직.\n")


def main() -> None:
    cutoff = (datetime.now(KST).date()) - timedelta(days=LOOKBACK_DAYS)
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
            "rsi_band": [RSI_LO, RSI_HI],
            "sma": [SMA_FAST, SMA_SLOW],
        },
        "basket": [
            {"symbol": rs.symbol, "name": rs.name, "market": rs.market, "volume": rs.volume}
            for rs in basket
        ],
        "eval_days": result["eval_days"],
        "results": {
            "baseline": {"mean_pct": base.mean_pct, "win_rate_pct": base.win_rate_pct, "std_pct": base.std_pct},
            "improved": {"mean_pct": impr.mean_pct, "win_rate_pct": impr.win_rate_pct, "std_pct": impr.std_pct},
            "pool": {"mean_pct": pool.mean_pct, "win_rate_pct": pool.win_rate_pct, "std_pct": pool.std_pct},
        },
        "paired_mean_diff_pct": result["paired_mean_diff_pct"],
        "paired_t": result["paired_t"],
        "caveats": "open->close approx, no costs, fixed basket, survivorship, small sample; directional only",
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"📄 결과 저장: {out_path}")


if __name__ == "__main__":
    main()
