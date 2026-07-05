"""선정 알파 검증 — 실제 발굴 기록 기반 "거래량 상위 매수" 엣지 실측 (read-only).

무엇/왜: selection_study.py(가상 바스켓·재정렬 가설)의 확장. 이번엔 가상이 아니라
**실제 발굴 기록**(data/reports/daily-*.json 의 rank_candidates/discovery_chosen)을
쓴다. 2026-07-04 적대적 검토·진입 축 검증의 최종 질문 — 출구·타이밍·쿨다운이 아니라
"아침 거래량 상위 종목을 산다"는 선정 자체에 우위가 있는가 — 에 답한다.

세 가지 질문:
1. 풀 알파 — 발굴 후보의 당일 시가→종가 수익률이 같은 날 KODEX 200 을 이기는가
   (후보가 기록된 전 일자 — 게이트 차단일 포함, 차단/거래일 분리 표기).
2. 순위 정보력 — 후보 풀 안에서 거래량 순위 상위(1~3, 실제 픽 위치)가
   하위(4~10)보다 나은가 (풀 10개가 기록된 날만).
3. 분봉 정밀 검증 — 일봉 시가→종가 근사가 실제 진입 창(09:10→15:15)과 다른 결론을
   내진 않는가 (분봉 확보 가능한 2026-06-15 이후만).

한계(정직 고지):
- 표본 작음 (후보 기록 21일 · 후보-일 ~100행 · 풀 10 기록일은 5일뿐).
- 시가→종가는 buy-and-hold 근사 — 실제 봇의 출구 규칙(트레일링 등)과 다르다.
  여기서 재는 건 '종목 선정의 원재료 수익률'이지 전략 손익이 아니다.
- 왕복비용 ~0.33% 를 넘어야 실전 의미 — 표에 비용 차감 전/후 병기.
- 상장폐지·조회불가 종목은 스킵하고 건수를 표기 (생존편향 방향: 알파 과대평가).

실행: uv run python scripts/diagnostics/selection_alpha_study.py
"""

from __future__ import annotations

import asyncio
import glob
import json
import statistics
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path

from entry_axes_study import fetch_minute_closes

from auto_trading.kiwoom.rest import KiwoomRestClient
from auto_trading.market.chart import get_daily_bars

MARKET_PROXY = "069500"  # KODEX 200 — 시장 통제군
ROUND_TRIP_COST_PCT = 0.33  # 왕복비용 근사 (reports/daily 상수 합)
POOL_TOP = 3  # 실제 픽 위치 (top_n=3)
MINUTE_SINCE = date(2026, 6, 15)  # 분봉 정밀 검증 구간 시작
ENTRY_AT = time(9, 10)
EXIT_AT = time(15, 15)


@dataclass(frozen=True)
class CandidateDay:
    day: date
    symbol: str
    rank: int  # rank_candidates 내 순서 (0-base) — 거래량 내림차순 가정
    chosen: bool
    day_traded: bool  # 그날 chosen 이 1개 이상 있었나 (게이트 차단일 구분)
    pool_size: int


def load_candidate_days() -> list[CandidateDay]:
    rows: list[CandidateDay] = []
    for p in sorted(glob.glob("data/reports/daily-*.json")):
        d = json.loads(Path(p).read_text())
        candidates = d.get("rank_candidates") or []
        if not candidates:
            continue
        chosen = set(d.get("discovery_chosen") or [])
        day = date.fromisoformat(Path(p).name[6:16])
        for i, symbol in enumerate(candidates):
            rows.append(
                CandidateDay(
                    day=day,
                    symbol=str(symbol),
                    rank=i,
                    chosen=symbol in chosen,
                    day_traded=bool(chosen),
                    pool_size=len(candidates),
                )
            )
    return rows


async def fetch_open_close(
    client: KiwoomRestClient, symbol: str
) -> dict[date, tuple[float, float]]:
    """일봉 → {일자: (시가, 종가)}. 조회 실패·빈 응답은 빈 dict."""
    try:
        bars = await get_daily_bars(client, symbol, max_pages=2)
    except Exception:
        return {}
    return {b.ts.date(): (b.open, b.close) for b in bars if b.open > 0 and b.close > 0}


def _summ(label: str, vals: list[float]) -> str:
    if not vals:
        return f"{label:<26} 표본 0"
    mean = statistics.mean(vals)
    med = statistics.median(vals)
    wins = sum(1 for v in vals if v > 0)
    net = mean - ROUND_TRIP_COST_PCT
    return (
        f"{label:<26} n={len(vals):>3}  평균 {mean:+6.2f}%  중앙 {med:+6.2f}%"
        f"  승률 {wins}/{len(vals)}  비용차감 {net:+6.2f}%"
    )


async def main() -> None:
    rows = load_candidate_days()
    if not rows:
        print("발굴 기록 없음 — data/reports/daily-*.json 확인.")
        return
    days = sorted({r.day for r in rows})
    symbols = sorted({r.symbol for r in rows})
    print(
        f"발굴 기록: {len(days)}일 / 후보-일 {len(rows)}행 / 고유 종목 {len(symbols)}개"
        f" ({days[0]} ~ {days[-1]})"
    )
    print("⚠️ 표본이 작다 — 방향 참고용. 시가→종가는 선정 원재료 수익률(전략 손익 아님).")
    print()

    oc: dict[str, dict[date, tuple[float, float]]] = {}
    async with KiwoomRestClient() as client:
        for symbol in [*symbols, MARKET_PROXY]:
            oc[symbol] = await fetch_open_close(client, symbol)

        # ── 1. 풀 알파: 후보 vs KODEX 200 (당일 시가→종가, 시장 대비 초과) ──
        print("=" * 76)
        print("1. 풀 알파 — 발굴 후보의 당일 시가→종가 vs KODEX 200 (초과수익률 %p)")
        print("=" * 76)
        raw_rets: list[float] = []
        excess_all: list[float] = []
        excess_traded: list[float] = []
        excess_blocked: list[float] = []
        chosen_excess: list[float] = []
        chosen_abs: list[float] = []
        skipped = 0
        for r in rows:
            pair = oc.get(r.symbol, {}).get(r.day)
            mkt = oc.get(MARKET_PROXY, {}).get(r.day)
            if pair is None or mkt is None:
                skipped += 1
                continue
            ret = (pair[1] - pair[0]) / pair[0] * 100
            mret = (mkt[1] - mkt[0]) / mkt[0] * 100
            raw_rets.append(ret)
            excess = ret - mret
            excess_all.append(excess)
            (excess_traded if r.day_traded else excess_blocked).append(excess)
            if r.chosen:
                chosen_excess.append(excess)
                chosen_abs.append(ret)
        print(_summ("후보 절대수익(시가→종가)", raw_rets))
        print(_summ("후보 초과수익(vs KODEX)", excess_all))
        print(_summ("  └ 거래일(게이트 통과)", excess_traded))
        print(_summ("  └ 차단일(게이트 차단)", excess_blocked))
        print(_summ("실제 픽만 초과수익", chosen_excess))
        print(_summ("실제 픽 절대수익", chosen_abs))
        if skipped:
            print(f"(조회불가 스킵 {skipped}행 — 생존편향: 알파 과대평가 방향)")
        print("읽는 법: 초과수익 평균이 +비용(0.33%) 이상이어야 '선정 엣지 있음'.")
        print()

        # ── 2. 순위 정보력: 풀 10 기록일에서 상위 1~3 vs 4~10 ──
        print("=" * 76)
        print(
            f"2. 순위 정보력 — 거래량 순위 상위 1~{POOL_TOP} vs {POOL_TOP + 1}~10 (풀 10 기록일만)"
        )
        print("=" * 76)
        top_excess: list[float] = []
        rest_excess: list[float] = []
        pool_days = sorted({r.day for r in rows if r.pool_size >= 10})
        for r in rows:
            if r.pool_size < 10:
                continue
            pair = oc.get(r.symbol, {}).get(r.day)
            mkt = oc.get(MARKET_PROXY, {}).get(r.day)
            if pair is None or mkt is None:
                continue
            excess = (pair[1] - pair[0]) / pair[0] * 100 - (mkt[1] - mkt[0]) / mkt[0] * 100
            (top_excess if r.rank < POOL_TOP else rest_excess).append(excess)
        print(f"대상일: {len(pool_days)}일 ({', '.join(str(d) for d in pool_days)})")
        print(_summ(f"상위 1~{POOL_TOP} (픽 위치)", top_excess))
        print(_summ(f"하위 {POOL_TOP + 1}~10", rest_excess))
        print()

        # ── 3. 분봉 정밀 검증: 09:10→15:15 vs 일봉 근사 (6/15 이후) ──
        print("=" * 76)
        print(f"3. 분봉 정밀 검증 — 09:10→15:15 실측 ({MINUTE_SINCE} 이후, 일봉 근사 교차확인)")
        print("=" * 76)
        recent = [r for r in rows if r.day >= MINUTE_SINCE]
        recent_syms = sorted({r.symbol for r in recent})
        minute_excess: list[float] = []
        minute_skipped = 0
        mkt_minutes = await fetch_minute_closes(client, MARKET_PROXY, oldest_day=MINUTE_SINCE)
        sym_minutes: dict[str, dict[date, list]] = {}
        for symbol in recent_syms:
            try:
                sym_minutes[symbol] = await fetch_minute_closes(
                    client, symbol, oldest_day=MINUTE_SINCE
                )
            except Exception:
                sym_minutes[symbol] = {}

        def window_return(series: list, entry: time, exit_: time) -> float | None:
            entry_bars = [p for t, p in series if entry <= t.time() < exit_]
            exit_bars = [p for t, p in series if t.time() >= exit_]
            if not entry_bars or not exit_bars or entry_bars[0] <= 0:
                return None
            return (exit_bars[0] - entry_bars[0]) / entry_bars[0] * 100

        for r in recent:
            series = sym_minutes.get(r.symbol, {}).get(r.day)
            mkt_series = mkt_minutes.get(r.day)
            if not series or not mkt_series:
                minute_skipped += 1
                continue
            ret = window_return(series, ENTRY_AT, EXIT_AT)
            mret = window_return(mkt_series, ENTRY_AT, EXIT_AT)
            if ret is None or mret is None:
                minute_skipped += 1
                continue
            minute_excess.append(ret - mret)
        print(_summ("후보 초과수익(분봉 실측)", minute_excess))
        if minute_skipped:
            print(f"(분봉 미확보 스킵 {minute_skipped}행)")
        print("일봉 근사(1번 표)와 부호·크기가 크게 다르면 근사를 불신할 것.")


if __name__ == "__main__":
    asyncio.run(main())
