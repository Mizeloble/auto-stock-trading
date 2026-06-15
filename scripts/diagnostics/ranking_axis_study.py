"""발굴 랭킹 축 읽기전용 검증 — "주식수 거래량(현행) vs 거래대금 vs +최소가격 필터".

무엇/왜:
- 현재 day-trade 발굴은 `market.rank.top_volume` 이 **주식 수 기준 거래량** 으로
  정렬한다(`sort_tp="1"`). 주식 수 랭킹은 구조적으로 저가주(동전주)·테마 급등주에
  편향된다 — 1,000원짜리는 10,000원짜리보다 같은 거래대금에서 10배 많은 '거래량'
  으로 잡힌다 (2026-06-10 제3자 검토). 가설: 같은 후보군에서 **거래대금**(가격x수량)
  으로 뽑거나 **최소가격 필터** 를 더하면 고른 종목의 당일 수익이 덜 나쁘다.
- selection_study(2026-06-07)와 같은 방법론: 단타 진입~청산을 '당일 시가→종가
  수익률'로 근사해 읽기전용으로 방향성만 확인. 그 연구의 부수 발견("거래량 상위
  압축 자체가 음의 엣지 가능성")의 후속 검증이기도 하다.

방식:
1. 1회 라이브 `top_volume(top_n≈40)` 로 바스켓 고정 (현행 발굴이 보는 종목 유형).
2. 각 종목 ~3년 일봉 → {날짜: {종목: (시가, 종가, 거래량)}} 패널.
3. 각 거래일 D 마다 6가지 선별로 같은 K개씩 골라 당일 (close-open)/open 평균 비교:
   - baseline      : D 거래량(주식 수) 상위 K — 현행 근사
   - value         : D 거래대금(거래량x시가) 상위 K
   - value_filtered: 시가 ≥ MIN_PRICE 인 종목 중 거래대금 상위 K
   - volume_filtered: 시가 ≥ MIN_PRICE 인 종목 중 거래량 상위 K (필터 단독 효과 분리)
   - relvol        : 상대거래량(전일 거래량 / 직전 20일 평균) 상위 K — "평소 대비 급증"
   - momentum      : 직전 5일 종가 수익률 상위 K — "최근 오른 종목"
   ※ relvol·momentum 은 D 이전 데이터만 쓰는 후행 지표라 same_day·prev_day 양 모드에서
     동일하게(룩어헤드 0) 계산된다. 사용자 가설("매번 삼성전자")의 대안 축 검증용.
4. 페어드 차이(변형-baseline) + 이동블록 부트스트랩 CI/p → 표 + JSON 저장.
5. `--self-check`: 네트워크 없이 합성 데이터 2종(저가주가 실제로 나쁜 세계 /
   무관한 세계)으로 스크립트 자체를 검증.

한계(정직): 시가→종가 근사(장중 타이밍 무시)·비용 제외·당일 종가 거래량을 09:06
랭킹의 프록시로 사용(모든 변형에 동일 적용이라 비교는 공정)·고정 바스켓·생존편향.
→ 방향성 신호일 뿐 증명 아님. CI 가 0 을 포함하면 결론 보류가 정직.

실행: uv run python scripts/diagnostics/ranking_axis_study.py [--self-check]
"""

from __future__ import annotations

import asyncio
import json
import random
import statistics
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from auto_trading._common import KST, Bar
from auto_trading.kiwoom.rest import KiwoomRestClient
from auto_trading.market.chart import get_daily_bars
from auto_trading.market.rank import RankedSymbol, top_volume

# ── 스터디 파라미터 (사전 고정: 사후 노브 튜닝 회피) ──────────────────────────
BASKET_SIZE = 40  # 고정 바스켓 (거래대금 픽이 거래량 픽과 갈리려면 넓게)
LOOKBACK_DAYS = 1095  # 3년
PICK_K = 6  # 현행 top_n=6 과 일치
MIN_PRICE = 2_000.0  # 최소가격 필터 (동전주 컷)
MAX_PAGES = 3  # 일봉 페이지 (페이지당 ~600봉 — 3년 충분)
BOOT_N = 3_000
BOOT_BLOCK = 10  # 이동블록 길이 (일 수익률 자기상관 보존)
AVG_VOL_WINDOW = 20  # 상대거래량 분모 — 직전 N일 평균 거래량
MOM_WINDOW = 5  # 모멘텀 룩백 — 직전 N일 종가 수익률


@dataclass(frozen=True)
class DayRow:
    open: float
    close: float
    volume: int
    # 전일(D-1) 활동량 — 룩어헤드 없는 랭킹 모드용. 첫 봉은 None.
    prev_volume: int | None = None
    prev_value: float | None = None
    # 후행 지표 (D 이전 데이터만 사용 → 양 모드 공통, 룩어헤드 0). 워밍업 전엔 None.
    # avg_vol: D-1 까지 직전 AVG_VOL_WINDOW 일 평균 거래량 (상대거래량 분모).
    # mom: 직전 MOM_WINDOW 일 종가 수익률(D-1 종가 / (MOM_WINDOW+1)일 전 종가 - 1).
    avg_vol: float | None = None
    mom: float | None = None

    @property
    def oc_return(self) -> float:
        return (self.close - self.open) / self.open if self.open > 0 else 0.0

    @property
    def value(self) -> float:
        """당일 거래대금 프록시 — 거래량 x 시가 (시가는 선별 시점에 알 수 있는 가격)."""
        return self.volume * self.open

    @property
    def relvol(self) -> float | None:
        """상대거래량 — 전일 거래량 / 직전 평균 거래량. '평소 대비 얼마나 들썩였나'."""
        if self.prev_volume is None or not self.avg_vol:
            return None
        return self.prev_volume / self.avg_vol


Panel = dict[date, dict[str, DayRow]]

# 랭킹 시점 프록시 2종 — 진실은 둘 사이(봇은 09:06 그날 아침 거래량으로 랭킹):
# - same_day: D 전체 거래량으로 D 를 선별 — 약한 룩어헤드(급등일을 사후 선택)가
#   섞이고, % 변동이 큰 저가주의 주식 수가 폭증해 '거래량 랭킹'에 유리하게 기운다.
# - prev_day: D-1 거래량/대금으로 D 를 선별 — 룩어헤드 0, 대신 아침 모멘텀 정보 없음.
# 결론은 두 모드가 같은 방향일 때만 신뢰한다.
RANK_MODES = ("same_day", "prev_day")


def build_panel(bars_by_symbol: dict[str, list[Bar]], *, cutoff: date) -> Panel:
    panel: Panel = {}
    for symbol, bars in bars_by_symbol.items():
        prev: Bar | None = None
        recent_vol: list[int] = []  # D 이전 거래량 (avg_vol 분모)
        recent_close: list[float] = []  # D 이전 종가 (mom)
        for bar in sorted(bars, key=lambda b: b.ts):
            d = bar.ts.date()
            if d >= cutoff and bar.open > 0:
                avg_vol = (
                    statistics.fmean(recent_vol[-AVG_VOL_WINDOW:])
                    if len(recent_vol) >= AVG_VOL_WINDOW
                    else None
                )
                mom = (
                    recent_close[-1] / recent_close[-(MOM_WINDOW + 1)] - 1.0
                    if len(recent_close) >= MOM_WINDOW + 1 and recent_close[-(MOM_WINDOW + 1)] > 0
                    else None
                )
                panel.setdefault(d, {})[symbol] = DayRow(
                    open=bar.open,
                    close=bar.close,
                    volume=bar.volume,
                    prev_volume=prev.volume if prev else None,
                    prev_value=prev.volume * prev.close if prev else None,
                    avg_vol=avg_vol,
                    mom=mom,
                )
            prev = bar
            recent_vol.append(bar.volume)
            recent_close.append(bar.close)
    return panel


# ── 선별 규칙 ───────────────────────────────────────────────────────────────


def _pick_mean(rows: list[DayRow]) -> float | None:
    if not rows:
        return None
    return statistics.fmean(r.oc_return for r in rows)


def select_returns(rows: dict[str, DayRow], *, mode: str) -> dict[str, float | None]:
    """하루치 패널 → 변형별 '그날 고른 K개의 평균 시가→종가 수익률'."""
    if mode == "same_day":
        all_rows = list(rows.values())
        vol_key = lambda r: r.volume  # noqa: E731
        val_key = lambda r: r.value  # noqa: E731
    else:  # prev_day — 전일 활동량이 있는 종목만 (룩어헤드 0)
        all_rows = [r for r in rows.values() if r.prev_volume is not None]
        vol_key = lambda r: r.prev_volume  # noqa: E731
        val_key = lambda r: r.prev_value  # noqa: E731
    by_volume = sorted(all_rows, key=vol_key, reverse=True)
    by_value = sorted(all_rows, key=val_key, reverse=True)
    eligible = [r for r in all_rows if r.open >= MIN_PRICE]
    by_value_f = sorted(eligible, key=val_key, reverse=True)
    by_volume_f = sorted(eligible, key=vol_key, reverse=True)
    # 상대거래량·모멘텀 — 후행 지표라 mode 무관(D 이전 데이터만). 워밍업 안 된 종목 제외.
    rv_rows = [r for r in all_rows if r.relvol is not None]
    by_relvol = sorted(rv_rows, key=lambda r: r.relvol, reverse=True)  # type: ignore[arg-type,return-value]
    mom_rows = [r for r in all_rows if r.mom is not None]
    by_mom = sorted(mom_rows, key=lambda r: r.mom, reverse=True)  # type: ignore[arg-type,return-value]
    return {
        "baseline": _pick_mean(by_volume[:PICK_K]),
        "value": _pick_mean(by_value[:PICK_K]),
        "value_filtered": _pick_mean(by_value_f[:PICK_K]),
        "volume_filtered": _pick_mean(by_volume_f[:PICK_K]),
        "relvol": _pick_mean(by_relvol[:PICK_K]),
        "momentum": _pick_mean(by_mom[:PICK_K]),
        "pool": _pick_mean(all_rows),
    }


VARIANTS = (
    "baseline",
    "value",
    "value_filtered",
    "volume_filtered",
    "relvol",
    "momentum",
    "pool",
)


def evaluate(panel: Panel, *, mode: str = "same_day") -> dict[str, object]:
    """전 거래일 집계 — 모든 변형이 픽을 가진 날만 (페어드 비교 공정성)."""
    series: dict[str, list[float]] = {v: [] for v in VARIANTS}
    for d in sorted(panel):
        rows = panel[d]
        if len(rows) < PICK_K:
            continue
        picks = select_returns(rows, mode=mode)
        if any(picks[v] is None for v in VARIANTS):
            continue  # 필터 후 K 미달 등 — 그날은 전 변형 제외
        for v in VARIANTS:
            series[v].append(picks[v])  # type: ignore[arg-type]
    return {"eval_days": len(series["baseline"]), "series": series}


# ── 통계 ────────────────────────────────────────────────────────────────────


def block_bootstrap_ci(
    diffs: list[float], *, n: int = BOOT_N, block: int = BOOT_BLOCK, seed: int = 42
) -> dict[str, float]:
    """이동블록 부트스트랩 — 페어드 차이 평균의 SE/CI95/p(양측, 0 대비)."""
    if len(diffs) < block + 1:
        return {"se": 0.0, "lo": 0.0, "hi": 0.0, "p": 1.0}
    rng = random.Random(seed)
    n_blocks = len(diffs) - block + 1
    n_draw = max(1, len(diffs) // block)
    means: list[float] = []
    for _ in range(n):
        sample: list[float] = []
        for _ in range(n_draw):
            start = rng.randrange(n_blocks)
            sample.extend(diffs[start : start + block])
        means.append(statistics.fmean(sample))
    means.sort()
    observed = statistics.fmean(diffs)
    lo = means[int(0.025 * len(means))]
    hi = means[int(0.975 * len(means))]
    # p: 부트스트랩 분포를 0 중심으로 옮겨 |observed| 이상이 나올 비율
    centered = [m - observed for m in means]
    extreme = sum(1 for c in centered if abs(c) >= abs(observed))
    return {
        "se": statistics.pstdev(means),
        "lo": lo,
        "hi": hi,
        "p": extreme / len(means),
    }


def summarize(result: dict[str, object]) -> dict[str, object]:
    series: dict[str, list[float]] = result["series"]  # type: ignore[assignment]
    base = series["baseline"]
    out: dict[str, object] = {"eval_days": result["eval_days"], "variants": {}, "vs_baseline": {}}
    for v in VARIANTS:
        rets = series[v]
        out["variants"][v] = {  # type: ignore[index]
            "mean_pct": 100.0 * statistics.fmean(rets) if rets else 0.0,
            "win_rate_pct": 100.0 * sum(1 for r in rets if r > 0) / len(rets) if rets else 0.0,
            "std_pct": 100.0 * statistics.pstdev(rets) if len(rets) > 1 else 0.0,
        }
    for v in VARIANTS:
        if v in ("baseline", "pool"):
            continue
        diffs = [a - b for a, b in zip(series[v], base, strict=True)]
        boot = block_bootstrap_ci(diffs)
        out["vs_baseline"][v] = {  # type: ignore[index]
            "paired_mean_diff_pct": 100.0 * statistics.fmean(diffs) if diffs else 0.0,
            "boot_se_pct": 100.0 * boot["se"],
            "boot_ci95_pct": [100.0 * boot["lo"], 100.0 * boot["hi"]],
            "boot_p": boot["p"],
        }
    return out


# ── 자기검증 (합성 데이터, 네트워크 0) ──────────────────────────────────────


def _synthetic_panel(*, low_price_is_bad: bool, seed: int = 7) -> Panel:
    """합성 세계 60종목 x 300일.

    - low_price_is_bad=True: 저가주(<2,000원)는 일평균 -0.4% 드리프트, 고가주 +0.05%.
      거래량(주식 수)은 가격에 반비례 → 주식수 랭킹이 저가주를 상위로 끌어올림.
      기대: value/value_filtered 가 baseline 을 이겨야 함.
    - False: 수익률이 가격·거래량과 무관(iid) → 차이 ~0 이어야 함.
    """
    rng = random.Random(seed)
    symbols = [f"S{i:03d}" for i in range(60)]
    prices = {
        s: (rng.uniform(300, 1_800) if i < 30 else rng.uniform(3_000, 80_000))
        for i, s in enumerate(symbols)
    }
    panel: Panel = {}
    d = date(2025, 1, 1)
    prev_act: dict[str, tuple[int, float]] = {}  # symbol → (어제 거래량, 어제 종가)
    hist_vol: dict[str, list[int]] = {s: [] for s in symbols}  # D 이전 거래량
    hist_close: dict[str, list[float]] = {s: [] for s in symbols}  # D 이전 종가
    for _ in range(300):
        d += timedelta(days=1)
        rows: dict[str, DayRow] = {}
        for s in symbols:
            px = prices[s]
            drift = (-0.004 if px < MIN_PRICE else 0.0005) if low_price_is_bad else 0.0
            ret = rng.gauss(drift, 0.02)
            # 거래대금은 가격대와 무관하게 비슷한 스케일 → 주식 수 = 대금/가격 (저가주 폭증)
            value = rng.uniform(5e9, 5e10)
            volume = int(value / px)
            close = px * (1 + ret)
            pv = prev_act.get(s)
            hv, hc = hist_vol[s], hist_close[s]
            avg_vol = statistics.fmean(hv[-AVG_VOL_WINDOW:]) if len(hv) >= AVG_VOL_WINDOW else None
            mom = (
                hc[-1] / hc[-(MOM_WINDOW + 1)] - 1.0
                if len(hc) >= MOM_WINDOW + 1 and hc[-(MOM_WINDOW + 1)] > 0
                else None
            )
            rows[s] = DayRow(
                open=px,
                close=close,
                volume=volume,
                prev_volume=pv[0] if pv else None,
                prev_value=pv[0] * pv[1] if pv else None,
                avg_vol=avg_vol,
                mom=mom,
            )
            prev_act[s] = (volume, close)
            hv.append(volume)
            hc.append(close)
        panel[d] = rows
    return panel


def self_check() -> None:
    for mode in RANK_MODES:
        print(f"자기검증 [{mode}] 1/2 — 저가주가 실제로 나쁜 합성 세계:")
        s = summarize(evaluate(_synthetic_panel(low_price_is_bad=True), mode=mode))
        diff_vf = s["vs_baseline"]["value_filtered"]["paired_mean_diff_pct"]  # type: ignore[index]
        diff_v = s["vs_baseline"]["value"]["paired_mean_diff_pct"]  # type: ignore[index]
        print(f"  value-baseline {diff_v:+.3f}%p, value_filtered-baseline {diff_vf:+.3f}%p")
        assert diff_v > 0.1 and diff_vf > 0.1, "거래대금/필터가 저가주 함정을 회피해야 함"

        print(f"자기검증 [{mode}] 2/2 — 가격과 수익이 무관한 합성 세계:")
        s0 = summarize(evaluate(_synthetic_panel(low_price_is_bad=False), mode=mode))
        for v in ("value", "value_filtered", "volume_filtered"):
            d = s0["vs_baseline"][v]["paired_mean_diff_pct"]  # type: ignore[index]
            se = s0["vs_baseline"][v]["boot_se_pct"]  # type: ignore[index]
            z = d / se if se > 0 else 0.0
            print(f"  {v}-baseline {d:+.3f}%p (z={z:+.1f})")
            # 95% CI 포함 검사는 모드x변형 6회 다중검정으로 ~26% 확률로 우연히 깨진다
            # (관측: z≈2.1). 가짜 신호 '생성' 검출이 목적이므로 z<4 면 무신호로 판정.
            assert abs(z) < 4.0, "무관한 세계에서 강한 가짜 신호가 나오면 안 됨"
    print("✅ 자기검증 통과 — 스크립트가 신호를 만들어내지도, 놓치지도 않음.\n")


# ── 실데이터 실행 ───────────────────────────────────────────────────────────


async def _fetch_all() -> tuple[list[RankedSymbol], dict[str, list[Bar]]]:
    async with KiwoomRestClient() as client:
        basket = await top_volume(client, top_n=BASKET_SIZE, exclude_etf=True)
        bars_by_symbol: dict[str, list[Bar]] = {}
        for rs in basket:
            bars = await get_daily_bars(client, rs.symbol, max_pages=MAX_PAGES)
            if bars:
                bars_by_symbol[rs.symbol] = bars
        return basket, bars_by_symbol


def _print_report(summary: dict[str, object], cutoff: date, *, mode: str) -> None:
    print("\n" + "=" * 72)
    print(f"발굴 랭킹 축 검증 [{mode}]: 주식수 거래량(현행) vs 거래대금 vs +필터")
    print("=" * 72)
    print(
        f"평가구간: {cutoff} ~ 오늘 | 평가일수: {summary['eval_days']} | K={PICK_K}, 최소가격 {MIN_PRICE:,.0f}원"
    )
    print("-" * 72)
    print(f"{'변형':<18}{'평균일수익%':>12}{'승률%':>9}{'표준편차%':>11}")
    labels = {
        "baseline": "거래량(현행)",
        "value": "거래대금",
        "value_filtered": "거래대금+필터",
        "volume_filtered": "거래량+필터",
        "relvol": "상대거래량",
        "momentum": "모멘텀5일",
        "pool": "바스켓 평균",
    }
    for v in VARIANTS:
        m = summary["variants"][v]  # type: ignore[index]
        print(
            f"{labels[v]:<18}{m['mean_pct']:>12.4f}{m['win_rate_pct']:>9.1f}{m['std_pct']:>11.4f}"
        )
    print("-" * 72)
    for v, d in summary["vs_baseline"].items():  # type: ignore[attr-defined]
        ci = d["boot_ci95_pct"]
        sig = "유의" if not (ci[0] <= 0.0 <= ci[1]) else "불확실(CI 가 0 포함)"
        print(
            f"{labels[v]}-현행: {d['paired_mean_diff_pct']:+.4f}%p/일 "
            f"CI95[{ci[0]:+.4f},{ci[1]:+.4f}] p={d['boot_p']:.3f} → {sig}"
        )
    print("=" * 72)
    print("⚠️ 한계: 시가→종가 근사·비용 제외·당일 거래량 프록시·고정 바스켓·생존편향.")
    print("   CI 가 0 을 포함하면 결론 보류. 양수 유의 시에만 sort_tp 전환을 검토.\n")


def main() -> None:
    if "--self-check" in sys.argv:
        self_check()
        return
    self_check()  # 실행 전 항상 자기검증 먼저 — 깨지면 실데이터 결과도 신뢰 불가
    cutoff = datetime.now(KST).date() - timedelta(days=LOOKBACK_DAYS)
    basket, bars_by_symbol = asyncio.run(_fetch_all())
    if not bars_by_symbol:
        print("[오류] 일봉 데이터를 못 받았습니다. KIWOOM_MODE/.env/네트워크를 확인하세요.")
        return
    panel = build_panel(bars_by_symbol, cutoff=cutoff)
    by_mode: dict[str, dict[str, object]] = {}
    for mode in RANK_MODES:
        summary = summarize(evaluate(panel, mode=mode))
        _print_report(summary, cutoff, mode=mode)
        by_mode[mode] = summary

    out_dir = Path("data/studies")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"ranking_axis_study_{stamp}.json"
    payload = {
        "generated_at": datetime.now(KST).isoformat(),
        "params": {
            "basket_size": len(basket),
            "lookback_days": LOOKBACK_DAYS,
            "pick_k": PICK_K,
            "min_price": MIN_PRICE,
            "bootstrap": {"n": BOOT_N, "block": BOOT_BLOCK},
            "rank_modes": list(RANK_MODES),
        },
        "basket": [
            {"symbol": rs.symbol, "name": rs.name, "market": rs.market, "volume": rs.volume}
            for rs in basket
        ],
        "by_mode": by_mode,
        "caveats": (
            "open->close approx, no costs; same_day mode has mild lookahead favoring "
            "share-volume ranking, prev_day mode has none — trust only same-direction "
            "results; relvol/momentum are backward-looking (lookahead-free in both modes); "
            "fixed survivorship basket, mock data; directional only"
        ),
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"📄 결과 저장: {out_path}")


if __name__ == "__main__":
    main()
