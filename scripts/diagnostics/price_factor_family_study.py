"""가격 파생 지표 계열(미분/적분) — 이중 패널 동시 검정 기록 (read-only).

무엇/왜: "지표를 바꾸면/섞으면 되지 않나" 계열 질문의 종결 기록. 가격의 미분
(모멘텀 1/5/20일·가속도)·적분(SMA20 갭·RSI14) 대표점 6개 x 양방향 = 12셀을
**독립 두 패널에서 동시 유의** 기준으로 검정한다 — 인샘플(2024-06~2026-08)과
OOS(2023-06~2024-06)가 모두 5% 유의해야 통과하므로 셀당 우연 통과 확률 ~0.25%,
12셀 기대 오탐 ~0.03건. 사후 부호 뒤집기·단일 기간 과최적화를 구조적으로 차단.

판정 결과 (최초 확정 2026-08-17): **12셀 전부 기각.** (표본이 자라면 재실행
수치는 소폭 이동하나 12셀 기각 판정은 동일 — 예: mom_1d/high -0.742 -> -0.998%p)
  - 두 패널에서 부호가 일관된 유일한 효과는 음(-)의 것: mom_1d/high(전일 급등주
    매수) 인샘플 -0.7~-1.0 / OOS -0.5~-0.7%p — 봇의 선정 원리('터진 종목 사기')가
    또 한 번 정죄됨. 거울상(전일 하락주 매수)은 패널 간 부호 불일치.
  - mom_20d/high 는 레짐 의존 실례: OOS(횡보장) CI 하한 +0.095 로 '통과' 처럼
    보이나 인샘플(강세장) -0.4~-0.5%p — 단일 패널 판정의 위험을 보여줌.
  - 미분/적분은 같은 가격 시계열의 재가중(새 정보 아님). 일봉 수준 가격 파생
    지표 계열은 이로써 대표점이 모두 커버됐다.

실행: uv run python scripts/diagnostics/price_factor_family_study.py
      (fdr-oos 캐시가 없으면 최초 1회 ~10분 적재)
"""

from __future__ import annotations

import statistics
import sys
from datetime import date
from pathlib import Path

from indicator_harness import build_universe, moving_block_ci

from auto_trading.strategy.indicators import rsi_wilder, sma

PANELS: dict[str, tuple[Path, date, date]] = {
    "인샘플": (Path("data/studies/cache/fdr"), date(2024, 6, 17), date(2026, 8, 16)),
    "OOS": (Path("data/studies/cache/fdr-oos"), date(2023, 6, 1), date(2024, 6, 14)),
}
OOS_FETCH_START = "2023-04-01"  # 워밍업 포함
COST = 0.316
PICK_K, MIN_HIST = 3, 25
FEATS = ("mom_1d", "mom_5d", "mom_20d", "accel_5d", "sma20_gap", "rsi14")


def _fetch_oos(symbol: str, cache: Path, end: date) -> None:
    """OOS 캐시 미존재 시 fdr 에서 1회 적재 — 인샘플 캐시는 indicator_harness 가 만든다."""
    try:
        import FinanceDataReader as fdr

        df = fdr.DataReader(symbol, OOS_FETCH_START, end.isoformat())
    except Exception:
        return
    lines = ["date,open,high,low,close,volume"]
    for idx, r in df.iterrows():
        try:
            o, h, low, c, v = (
                float(r["Open"]),
                float(r["High"]),
                float(r["Low"]),
                float(r["Close"]),
                float(r["Volume"]),
            )
        except KeyError, TypeError, ValueError, AttributeError:
            continue
        if o <= 0 or c <= 0 or v <= 0:
            continue
        lines.append(f"{idx.date().isoformat()},{o},{h},{low},{c},{v}")
    cache.mkdir(parents=True, exist_ok=True)
    (cache / f"{symbol}.csv").write_text("\n".join(lines), encoding="utf-8")


def _load_panel(label: str) -> dict[str, list[tuple[date, float, float]]]:
    """(day, open, close) — 이 검정은 종가 이력 + 당일 시가/종가만 쓴다."""
    cache, _start, end = PANELS[label]
    panel: dict[str, list[tuple[date, float, float]]] = {}
    for i, sym in enumerate(build_universe(), 1):
        p = cache / f"{sym}.csv"
        if not p.exists() and label == "OOS":
            _fetch_oos(sym, cache, end)
            if i % 50 == 0:
                print(f"  ... OOS 적재 {i}", file=sys.stderr)
        if not p.exists():
            continue
        rows = []
        for line in p.read_text(encoding="utf-8").splitlines()[1:]:
            c = line.split(",")
            if len(c) != 6:
                continue
            try:
                y, m, d = (int(x) for x in c[0].split("-"))
                rows.append((date(y, m, d), float(c[1]), float(c[4])))
            except ValueError:
                continue
        if len(rows) >= MIN_HIST + 5:
            panel[sym] = rows
    return panel


def _features(closes: list[float]) -> dict[str, float] | None:
    """closes = D-1 까지 오름차순. 부족하면 None. 룩어헤드 차단의 핵심 지점."""
    if len(closes) < MIN_HIST:
        return None
    c = closes[-1]
    c2, c6, c11, c21 = closes[-2], closes[-6], closes[-11], closes[-21]
    if min(c, c2, c6, c11, c21) <= 0:
        return None
    s20 = sma(closes, 20)
    r14 = rsi_wilder(closes[-20:], 14)
    if s20 is None or s20 <= 0 or r14 is None:
        return None
    return {
        "mom_1d": (c / c2 - 1) * 100,
        "mom_5d": (c / c6 - 1) * 100,
        "mom_20d": (c / c21 - 1) * 100,
        "accel_5d": (c / c6 - 1) * 100 - (c6 / c11 - 1) * 100,
        "sma20_gap": (c / s20 - 1) * 100,
        "rsi14": r14,
    }


def _run_panel(label: str):
    panel = _load_panel(label)
    _cache, start, end = PANELS[label]
    index = {s: {r[0]: i for i, r in enumerate(rows)} for s, rows in panel.items()}
    days_all = sorted({r[0] for rows in panel.values() for r in rows if start <= r[0] <= end})
    uni_d: dict[date, float] = {}
    cell: dict[tuple[str, str], dict[date, float]] = {
        (f, side): {} for f in FEATS for side in ("high", "low")
    }
    for day in days_all:
        feats_rows, outs = [], []
        for sym, rows in panel.items():
            pos = index[sym].get(day)
            if pos is None or pos < MIN_HIST:
                continue
            _d, o, c = rows[pos]
            if o <= 0:
                continue
            fv = _features([r[2] for r in rows[:pos]])
            if fv is None:
                continue
            feats_rows.append(fv)
            outs.append((c / o - 1) * 100)
        if len(outs) < 100 or all(abs(x) < 1e-12 for x in outs):
            continue
        uni_d[day] = statistics.fmean(outs)
        for f in FEATS:
            order = sorted(range(len(outs)), key=lambda i: -feats_rows[i][f])
            cell[(f, "high")][day] = statistics.fmean([outs[i] for i in order[:PICK_K]])
            cell[(f, "low")][day] = statistics.fmean([outs[i] for i in order[-PICK_K:]])
    return uni_d, cell


def main() -> None:
    results = {}
    for label in PANELS:
        uni_d, cell = _run_panel(label)
        days = sorted(uni_d)
        print(
            f"[{label}] 평가 {len(days)}일 · 유니버스 장중 드리프트 "
            f"{statistics.fmean([uni_d[d] for d in days]):+.3f}%/일"
        )
        results[label] = (uni_d, cell, days)
    print()
    print(
        f"{'특성/방향':<18}{'인샘플 알파':>12}{'IN CI하한':>10}{'OOS 알파':>10}{'OOS CI하한':>11}{'판정':>8}"
    )
    survivors = []
    for f in FEATS:
        for side in ("high", "low"):
            stats = {}
            for label, (uni_d, cell, days) in results.items():
                s = cell[(f, side)]
                a = [s[d] - uni_d[d] for d in days if d in s]
                picks = [s[d] for d in days if d in s]
                ci = moving_block_ci(a)
                stats[label] = (
                    statistics.fmean(a),
                    ci[0] if ci else float("nan"),
                    statistics.fmean(picks) - COST,
                )
            ain, lin, nin = stats["인샘플"]
            aoos, loos, noos = stats["OOS"]
            ok = lin > 0 and loos > 0 and nin > 0 and noos > 0
            if ok:
                survivors.append((f, side))
            print(
                f"{f + '/' + side:<18}{ain:>+11.3f}%{lin:>+10.3f}{aoos:>+9.3f}%{loos:>+10.3f}"
                f"{'✅채택후보' if ok else '❌':>9}"
            )
    print()
    if survivors:
        print(f"채택 후보 {len(survivors)}건: {survivors} — 반사실 정밀 검증으로 넘길 것.")
    else:
        print("-> 12셀 전부 기각 — 미분/적분 계열(일봉)에서 이중 패널을 통과한 지표 없음.")


if __name__ == "__main__":
    main()
