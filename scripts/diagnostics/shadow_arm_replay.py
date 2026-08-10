"""대안 발굴 축을 **실제 출구 스택으로** 재생 — 원재료가 아닌 실현손익 비교 (read-only).

무엇/왜:
    `rank_signal_ab_study.py` 는 09:10→15:15 buy-and-hold 로 발굴 '원재료' 를 비교한다.
    그런데 이 저장소의 확정 결론은 **"엣지는 오른꼬리인데 출구가 자른다"** 였다
    (선정 알파 검증). 즉 원재료가 좋아도 트레일링 0.8% 가 잘라 실현이 안 될 수 있다.
    그래서 A/B 가 채택 후보를 내면 "출구 규칙 반영 반사실" 을 한 번 더 통과해야 하는데,
    순매수 풀은 러너 구독 유니버스가 아니라 **틱·봉이 실시간으로 안 쌓인다.**

    이 스크립트가 그 공백을 메운다 — 분봉을 사후 조회해 각 축의 top_n 픽을 실제
    `PositionGuard`(손절·트레일링·부분익절)와 `ShadowTracker`(수수료·거래세·슬리피지·
    FIFO)로 재생한다. 두 컴포넌트 모두 운영과 **같은 클래스를 그대로 재사용** 하므로
    규칙이 재구현으로 어긋날 여지가 없다.

왜 라이브 섀도우가 아니라 재생인가(2026-08-10 조사 결과):
    러너의 섀도우 세션은 가상 계좌가 **하나**다(`_shadow_account_state`). 두 픽셋을 한
    트래커에 넣으면 일 매수한도 20M ÷ 종목당 7M = 3슬롯을 6종목이 다투게 되어 두 팔이
    서로를 잠식하고, 진행 중인 SMA 게이트 판정(거래량 픽 기준)까지 오염된다. 팔별
    분리는 러너의 계좌 배관(루프 전역 단일 `_account`)을 고쳐야 해 매매 핫패스를 건드린다.
    재생은 **축마다 독립 트래커**를 써서 이 잠식이 구조적으로 불가능하고, 이미 수집된
    과거 일자에도 소급 적용된다.

한계(정직 고지):
    - 진입 규칙은 "발굴 시각 이후 첫 봉에 전량 진입" 으로 **모든 축에 동일** 하게 적용한다.
      실세션의 모멘텀 게이트(0.35%/15분)는 재현하지 않는다 — 이 스크립트가 재는 것은
      *출구를 반영했을 때 축 간 차이* 이지 전략 전체의 손익이 아니다. 축이 같은 진입
      규칙을 쓰므로 비교는 공정하다.
    - 분봉은 요청 시점 기준 과거 며칠까지만 조회된다(실측 페이지당 900봉 ≈ 2~4거래일).
      오래된 일자는 결손으로 표기하고 건너뛴다 — **정기 실행이 필요**하다.
    - 체결가는 봉 종가 근사(슬리피지는 ShadowTracker 가 별도 반영).

실행:
    uv run python scripts/diagnostics/shadow_arm_replay.py            # 조회 가능한 전 일자
    uv run python scripts/diagnostics/shadow_arm_replay.py --days 5   # 최근 5일만
    uv run python scripts/diagnostics/shadow_arm_replay.py --selftest # API 없이 로직 검증
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path

from auto_trading._common import KST, Bar, Side
from auto_trading.daytrade.profile import DaytradeProfile
from auto_trading.kiwoom.rest import KiwoomRestClient
from auto_trading.market.chart import get_minute_bars
from auto_trading.risk.manager import RiskLimits
from auto_trading.risk.position_guard import PositionGuard
from auto_trading.shadow import ShadowSignal, ShadowTracker

ALT_RANK_PATH = Path("data/discovery/alt-rank.jsonl")
PROFILE_PATH = Path("strategies/profiles/daytrade-highvol.yaml")  # 실운영 프로파일
OUT_PATH = Path("data/studies/shadow-arm-replay.json")

ENTRY_AFTER = time(9, 10)  # 실세션 첫 진입 창
LIQUIDATE_AT = time(15, 15)  # EOD 청산
TOP_N = 3
ROUND_DISCOVERY = "0902"

AXIS_LABELS = {
    "volume": "거래량(현행)",
    "value": "거래대금",
    "foreign_netbuy": "외인순매수",
    "institution_netbuy": "기관순매수",
}


def _label(axis: str) -> str:
    return AXIS_LABELS.get(axis, axis)


@dataclass
class ArmResult:
    """한 축·하루의 재생 결과."""

    axis: str
    date: str
    realized_pnl: float = 0.0
    round_trips: int = 0
    exit_reasons: dict[str, int] = field(default_factory=dict)
    entered: list[str] = field(default_factory=list)
    missing_bars: list[str] = field(default_factory=list)


def load_picks(path: Path = ALT_RANK_PATH) -> dict[str, dict[str, list[str]]]:
    """alt-rank(1차 라운드) → {날짜: {축: [상위 TOP_N 종목]}}."""
    out: dict[str, dict[str, list[str]]] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(rec.get("round") or ROUND_DISCOVERY) != ROUND_DISCOVERY:
            continue  # 2차(09:30)는 진단용 — 실세션 진입 시각과 달라 섞지 않는다.
        day = str(rec.get("date") or "")
        if not day:
            continue
        axes: dict[str, list[str]] = {}
        base = _rows_to_symbols(rec.get("volume_pool_rows")) or [
            str(s) for s in rec.get("volume_pool") or []
        ]
        if base:
            axes["volume"] = base[:TOP_N]
        alt = rec.get("alt_pools")
        if isinstance(alt, dict):
            for name, rows in alt.items():
                syms = _rows_to_symbols(rows)
                if syms:
                    axes[str(name)] = syms[:TOP_N]
        elif rec.get("value_pool"):
            syms = _rows_to_symbols(rec.get("value_pool"))
            if syms:
                axes["value"] = syms[:TOP_N]
        if len(axes) >= 2:
            out[day] = axes
    return out


def _rows_to_symbols(rows: object) -> list[str]:
    if not isinstance(rows, list):
        return []
    return [str(r["symbol"]) for r in rows if isinstance(r, dict) and r.get("symbol")]


def load_limits(path: Path = PROFILE_PATH) -> tuple[RiskLimits, float]:
    """운영 프로파일 → (RiskLimits, 종목당 비중). 재구현 없이 YAML 단일 출처를 읽는다."""
    profile = DaytradeProfile.load_yaml(path)
    limits = RiskLimits()
    r = profile.risk
    limits.stop_loss_pct = r.stop_loss_pct
    limits.trailing_stop_pct = r.trailing_stop_pct
    limits.partial_take_pct = r.partial_take_pct
    limits.partial_take_qty_ratio = r.partial_take_qty_ratio
    limits.daily_notional_limit = r.daily_notional_limit
    limits.max_position_weight = r.max_position_weight
    return limits, r.max_position_weight


def replay_arm(
    axis: str,
    day: str,
    bars_by_symbol: dict[str, list[Bar]],
    *,
    limits: RiskLimits,
    position_weight: float,
    initial_cash: float,
) -> ArmResult:
    """한 축의 top_n 픽을 분봉으로 재생 — 진입 → 가드 → EOD 청산.

    축마다 **독립 트래커**라 다른 축의 매수가 이 축의 예산·한도를 잠식하지 않는다.
    """
    res = ArmResult(axis=axis, date=day)
    tracker = ShadowTracker(initial_cash=initial_cash, log_path=None)
    guard = PositionGuard(limits=limits)
    notional_used = 0.0

    # 진입: 모든 축 동일 규칙 — 발굴 시각 이후 첫 봉 종가에 매수.
    for symbol, bars in sorted(bars_by_symbol.items()):
        if not bars:
            res.missing_bars.append(symbol)
            continue
        entry = next((b for b in bars if b.ts.time() >= ENTRY_AFTER), None)
        if entry is None or entry.close <= 0:
            res.missing_bars.append(symbol)
            continue
        budget = initial_cash * position_weight
        if limits.daily_notional_limit > 0:
            budget = min(budget, limits.daily_notional_limit - notional_used)
        qty = int(budget // entry.close)
        if qty <= 0:
            continue  # 일 매수한도 소진 — 실세션과 동일하게 진입 포기.
        tracker.apply(ShadowSignal(symbol, Side.BUY, qty, entry.close, entry.ts))
        notional_used += qty * entry.close
        res.entered.append(symbol)

    # 출구: 봉을 시각순으로 합쳐 재생 — 종목 간 순서가 실세션과 같게.
    timeline: list[Bar] = sorted(
        (b for sym, bars in bars_by_symbol.items() if sym in res.entered for b in bars),
        key=lambda b: b.ts,
    )
    for bar in timeline:
        held = tracker.holdings.get(bar.symbol, 0)
        if held <= 0:
            continue
        if bar.ts.time() >= LIQUIDATE_AT:
            continue  # 청산은 아래에서 일괄 — 가드가 마지막 봉을 두 번 보지 않게.
        action = guard.check(
            symbol=bar.symbol,
            price=bar.close,
            quantity=held,
            avg_price=tracker.avg_prices.get(bar.symbol, 0.0),
        )
        if action is None or action.quantity <= 0:
            continue
        tracker.apply(ShadowSignal(bar.symbol, Side.SELL, action.quantity, bar.close, bar.ts))
        reason = str(action.reason)
        res.exit_reasons[reason] = res.exit_reasons.get(reason, 0) + 1
        res.round_trips += 1
        if tracker.holdings.get(bar.symbol, 0) <= 0:
            guard.reset(bar.symbol)

    # EOD 청산 — 남은 보유는 15:15 이후 첫 봉(없으면 마지막 봉) 종가로.
    for symbol in list(tracker.holdings):
        qty = tracker.holdings.get(symbol, 0)
        if qty <= 0:
            continue
        bars = bars_by_symbol.get(symbol) or []
        if not bars:
            continue
        liq = next((b for b in bars if b.ts.time() >= LIQUIDATE_AT), bars[-1])
        tracker.apply(ShadowSignal(symbol, Side.SELL, qty, liq.close, liq.ts))
        res.exit_reasons["liquidate"] = res.exit_reasons.get("liquidate", 0) + 1
        res.round_trips += 1

    res.realized_pnl = tracker.realized_pnl
    return res


class BarCache:
    """종목당 1회만 조회하고 날짜로 나눠 쓰는 캐시.

    한 번의 ka10080 응답이 2~4거래일을 덮으므로, (종목, 날짜) 마다 조회하면 같은
    데이터를 몇 번씩 다시 받는다 — 실측 429 폭주의 원인이었다. 종목 단위로 받아
    날짜별로 쪼개면 호출이 일수 배만큼 줄고 재시도도 사라진다.
    """

    def __init__(self, client: KiwoomRestClient) -> None:
        self._client = client
        self._by_symbol: dict[str, dict[date, list[Bar]]] = {}

    async def get(self, symbol: str, day: date) -> list[Bar]:
        if symbol not in self._by_symbol:
            try:
                bars = await get_minute_bars(self._client, symbol)
            except Exception:
                bars = []
            grouped: dict[date, list[Bar]] = {}
            for b in bars:
                grouped.setdefault(b.ts.date(), []).append(b)
            self._by_symbol[symbol] = grouped
        return self._by_symbol[symbol].get(day, [])

    @property
    def symbols_fetched(self) -> int:
        return len(self._by_symbol)


async def run(days: int | None) -> dict:
    picks = load_picks()
    if not picks:
        print(f"데이터 없음: {ALT_RANK_PATH} 에 비교 가능한 1차 레코드가 없습니다.")
        return {}
    limits, weight = load_limits()
    initial_cash = 100_000_000.0  # 모의계좌 리셋 후 평가액 (memory: 계좌 리셋 1억)
    dates = sorted(picks)[-days:] if days else sorted(picks)

    results: dict[str, list[ArmResult]] = {}
    skipped: list[str] = []
    print(f"=== 대안 축 출구 반영 재생 · 프로파일 {PROFILE_PATH.name} ===")
    print(
        f"손절 {limits.stop_loss_pct:.2%} · 트레일링 {limits.trailing_stop_pct:.2%} · "
        f"부분익절 {limits.partial_take_pct:.2%} · 일한도 {limits.daily_notional_limit:,.0f}원 · "
        f"종목당 {weight:.0%}\n"
    )
    # ka10080 은 기본 0.15s 페이싱으로도 429 가 잦다(실측) — 1.0s 로
    # 늦춘다. 진단 스크립트는 시간 여유가 크고, 헛재시도가 오히려 더 느리다.
    async with KiwoomRestClient(min_request_interval_sec=1.0) as client:
        cache = BarCache(client)
        # 최신 일자부터 처리 — 조회 범위 밖(오래된 일자)에서 헛호출을 덜 하게.
        for day in sorted(dates, reverse=True):
            axes = picks[day]
            universe = {s for syms in axes.values() for s in syms}
            target = date.fromisoformat(day)
            bars = {sym: await cache.get(sym, target) for sym in sorted(universe)}
            if not any(bars.values()):
                skipped.append(day)
                print(f"{day}  분봉 결손 — 조회 범위 밖(과거 일자), 건너뜀")
                continue
            day_res = [
                replay_arm(
                    axis,
                    day,
                    {s: bars.get(s, []) for s in syms},
                    limits=limits,
                    position_weight=weight,
                    initial_cash=initial_cash,
                )
                for axis, syms in sorted(axes.items())
            ]
            results[day] = day_res
            print(f"{day}")
            for r in day_res:
                reasons = ", ".join(f"{k}x{v}" for k, v in sorted(r.exit_reasons.items())) or "-"
                miss = f" · 봉결손 {len(r.missing_bars)}" if r.missing_bars else ""
                print(
                    f"    {_label(r.axis):<12}{r.realized_pnl:>12,.0f}원  "
                    f"진입 {len(r.entered)} · 왕복 {r.round_trips} · 출구: {reasons}{miss}"
                )
    return _summarize(results, skipped)


def _summarize(results: dict[str, list[ArmResult]], skipped: list[str]) -> dict:
    by_axis: dict[str, list[float]] = {}
    for day_res in results.values():
        for r in day_res:
            by_axis.setdefault(r.axis, []).append(r.realized_pnl)
    if not by_axis:
        print("\n재생된 일자가 없습니다 — 분봉 조회 범위 안에 수집일이 없습니다.")
        return {}

    print(f"\n--- 종합 ({len(results)}일" + (f", 결손 {len(skipped)}일)" if skipped else ")"))
    for axis, pnls in sorted(by_axis.items(), key=lambda kv: -sum(kv[1])):
        wins = sum(1 for p in pnls if p > 0)
        print(
            f"{_label(axis):<12} 합계 {sum(pnls):>12,.0f}원 · "
            f"평균 {statistics.fmean(pnls):>10,.0f}원 · 흑자 {wins}/{len(pnls)}일"
        )

    base = by_axis.get("volume")
    if base:
        print("\n--- 거래량(현행) 대비 (같은 날 페어드) ---")
        for axis, pnls in sorted(by_axis.items()):
            if axis == "volume" or len(pnls) != len(base):
                continue
            diffs = [a - b for a, b in zip(pnls, base, strict=True)]
            pos = sum(1 for d in diffs if d > 0)
            print(
                f"{_label(axis):<12} 평균 차 {statistics.fmean(diffs):>+12,.0f}원 · "
                f"우위 {pos}/{len(diffs)}일"
            )
    print(
        "\n주의: 진입 규칙은 전 축 동일(발굴 후 첫 봉 전량). 실세션의 모멘텀 게이트는"
        " 재현하지 않으므로 절대 손익이 아니라 **축 간 차이**만 읽을 것."
    )
    payload = {
        "generated_at": datetime.now(KST).isoformat(),
        "profile": PROFILE_PATH.name,
        "days": sorted(results),
        "skipped_days": skipped,
        "by_axis": {
            axis: {"total": sum(p), "mean": statistics.fmean(p), "days": len(p)}
            for axis, p in by_axis.items()
        },
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"저장: {OUT_PATH}")
    return payload


def selftest() -> None:
    """API 없이 재생 로직 검증 — 합성 분봉으로 손절·트레일링·청산이 발화하는지."""
    limits = RiskLimits()
    # RiskLimits 는 **비율** 단위(0.01 = 1%) — 퍼센트로 넣으면 100% 가 되어 영원히 무발화.
    limits.stop_loss_pct = 0.01
    limits.trailing_stop_pct = 0.008
    limits.partial_take_pct = 0.0
    limits.daily_notional_limit = 0.0
    day = datetime(2026, 8, 10, tzinfo=KST)

    def bar(hh: int, mm: int, price: float, symbol: str = "A") -> Bar:
        ts = day.replace(hour=hh, minute=mm)
        return Bar(symbol=symbol, ts=ts, open=price, high=price, low=price, close=price, volume=1)

    # 급락 → 손절 발화
    drop = [bar(9, 10, 1000.0), bar(9, 20, 1000.0), bar(9, 30, 980.0), bar(15, 20, 970.0)]
    res = replay_arm(
        "t", "2026-08-10", {"A": drop}, limits=limits, position_weight=0.07, initial_cash=1e8
    )
    assert res.entered == ["A"], res.entered
    assert "stop_loss" in res.exit_reasons, res.exit_reasons
    assert res.realized_pnl < 0

    # 상승 후 되돌림 → 트레일링 발화 + 이익 실현
    up = [bar(9, 10, 1000.0), bar(9, 20, 1100.0), bar(9, 30, 1080.0), bar(15, 20, 1000.0)]
    res2 = replay_arm(
        "t", "2026-08-10", {"A": up}, limits=limits, position_weight=0.07, initial_cash=1e8
    )
    assert "trailing" in res2.exit_reasons, res2.exit_reasons
    assert res2.realized_pnl > 0, res2.realized_pnl

    # 무발화 → 15:15 청산으로 반드시 정리(보유가 남으면 손익이 새어나간다)
    flat = [bar(9, 10, 1000.0), bar(9, 20, 1001.0), bar(15, 20, 1002.0)]
    res3 = replay_arm(
        "t", "2026-08-10", {"A": flat}, limits=limits, position_weight=0.07, initial_cash=1e8
    )
    assert res3.exit_reasons == {"liquidate": 1}, res3.exit_reasons

    # 일 매수한도 — 한도가 있으면 뒤 종목은 진입 포기(축 간 예산 잠식 없음 확인용)
    capped = RiskLimits()
    capped.daily_notional_limit = 1_000_000.0
    res4 = replay_arm(
        "t",
        "2026-08-10",
        {"A": [bar(9, 10, 1000.0), bar(15, 20, 1000.0)], "B": [bar(9, 10, 1000.0, "B")]},
        limits=capped,
        position_weight=0.07,
        initial_cash=1e8,
    )
    assert res4.entered == ["A"], res4.entered

    # 봉이 없는 종목은 결손으로 표기하고 건너뛴다(조용히 빠지지 않게)
    res5 = replay_arm(
        "t", "2026-08-10", {"A": []}, limits=limits, position_weight=0.07, initial_cash=1e8
    )
    assert res5.missing_bars == ["A"] and res5.round_trips == 0

    print("✅ selftest 통과 — 손절·트레일링·EOD청산·일한도·봉결손 분기 정상")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        n = None
        if "--days" in sys.argv:
            n = int(sys.argv[sys.argv.index("--days") + 1])
        asyncio.run(run(n))
