"""시계열(timeline) 한 줄 메시지 빌더.

발굴·매수·매도·청산·kill switch 를 시각순으로 모아 사람이 읽는 한국어 한 줄로
만든다. 매도 체결은 position_guard 이벤트와 (종목, 가격)으로 매칭해 사유를 라벨링.
"""

from __future__ import annotations

from typing import Any

from ..log_parser import LogEvents
from ._util import (
    _as_float,
    _as_int,
    _format_won_int,
    _guard_pct_part,
    _ts_to_kst_hhmm,
    _with_name_label,
)
from .models import TimelineEvent

# 가드 사유 라벨 — 매도 체결 timeline 줄에 붙는 사람용 한국어.
_GUARD_REASON_KO = {
    "partial_take": "부분 익절",
    "stop_loss": "🛑 손절",
    "trailing": "트레일링 청산",
}

_KILL_SWITCH_REASON_KO = {"daily_loss_kill": "일일 손실 한도"}
_LIQUIDATE_REASON_KO = {
    "liquidate_at_reached": "장 마감",
    "daily_loss_kill": "일일 손실 한도",
}


def _build_timeline(events: LogEvents, names: dict[str, str]) -> list[TimelineEvent]:
    """LogEvents → 시각 순으로 정렬된 한 줄 메시지 리스트.

    포함 이벤트(시각 있는 것만):
    - 발굴 완료 (daytrade.discovery)
    - 매수 체결 (runner.order_simulated / runner.order_accepted) — side=buy
    - 매도 체결 (runner.order_simulated / runner.order_accepted) — side=sell.
      가드 이벤트(손절·부분익절·트레일링)와 (종목, 가격)으로 매칭해 사유를 라벨링.
    - 청산 체결 (runner.liquidate_simulated / runner.liquidate_accepted)
    - kill switch (runner.kill_switch_triggered)
    - 청산 시작 (runner.liquidate_start)

    2026-06-15 회귀: 이전엔 side=buy 매수와 partial_take 가드만 timeline 에 넣어
    손절·트레일링·일반 매도·청산 체결이 통째로 빠졌다(매도 흐름 착시). 이제 매도/청산
    체결을 모두 시각순으로 넣고, 가드 이벤트로 사유를 붙인다. 체결 로그가 따로 없는
    가드 이벤트만 가드 자신의 정보로 폴백 한 줄을 emit.

    체결 가격은 시뮬에선 봉 종가 근사. 사용자에게 "실제 체결가가 아닐 수 있음" 은 코드 주석
    범위 — UI 에서 따로 표시하지 않음.
    """
    items: list[tuple[str, TimelineEvent]] = []  # (raw_ts, event) — 정렬 키 보존.

    def _add(raw_ts: object, kind: str, text: str) -> None:
        hhmm = _ts_to_kst_hhmm(raw_ts)
        if hhmm is None:
            return
        key = raw_ts if isinstance(raw_ts, str) else hhmm
        items.append((key, TimelineEvent(time_kst=hhmm, kind=kind, text=text)))

    if events.discovery is not None:
        chosen_raw = events.discovery.get("chosen") or []
        chosen = [str(s) for s in chosen_raw if isinstance(s, str)]
        suffix = ""
        if chosen:
            shown = ", ".join(_with_name_label(s, names) for s in chosen[:3])
            tail = f" 외 {len(chosen) - 3}" if len(chosen) > 3 else ""
            suffix = f": {shown}{tail}"
        _add(
            events.discovery.get("timestamp"),
            "discovery",
            f"발굴 채택 {len(chosen)}건{suffix}",
        )

    # 매수는 시뮬·실거래 모두 동일하게 한 줄.
    for rec in events.orders_simulated:
        if str(rec.get("side", "")) != "buy":
            continue
        symbol = str(rec.get("symbol", ""))
        qty = _as_int(rec.get("qty")) or 0
        price = _as_float(rec.get("price"))
        price_part = f" @{_format_won_int(price)}" if price is not None else ""
        _add(
            rec.get("timestamp"),
            "buy",
            f"매수 {_with_name_label(symbol, names)} {qty}주{price_part} (시뮬)",
        )
    for rec in events.orders_accepted:
        if str(rec.get("side", "")) != "buy":
            continue
        symbol = str(rec.get("symbol", ""))
        qty = _as_int(rec.get("qty")) or 0
        price = _as_float(rec.get("price"))
        price_part = f" @{_format_won_int(price)}" if price is not None else ""
        _add(
            rec.get("timestamp"),
            "buy",
            f"매수 {_with_name_label(symbol, names)} {qty}주{price_part}",
        )

    # 매도 체결을 사유(손절/부분익절/트레일링)로 라벨링하기 위한 가드 색인.
    # (종목, 체결가) → 가드 이벤트 큐. 체결과 1:1 로 소진하고, 남은 가드는 폴백 emit.
    guard_buckets: dict[tuple[str, float | None], list[tuple[str, dict[str, Any]]]] = {}
    for kind, guard_recs in (
        ("partial_take", events.partial_takes),
        ("stop_loss", events.stop_losses),
        ("trailing", events.trailings),
    ):
        for rec in guard_recs:
            symbol = str(rec.get("symbol", ""))
            price = _as_float(rec.get("price"))
            guard_buckets.setdefault((symbol, price), []).append((kind, rec))

    def _add_sell(rec: dict[str, Any], *, simulated: bool) -> None:
        symbol = str(rec.get("symbol", ""))
        qty = _as_int(rec.get("qty")) or 0
        price = _as_float(rec.get("price"))
        if not symbol or qty <= 0:
            return
        label = _with_name_label(symbol, names)
        price_part = f" @{_format_won_int(price)}" if price is not None else ""
        sim_part = " (시뮬)" if simulated else ""
        bucket = guard_buckets.get((symbol, price))
        if bucket:
            # 같은 종목·가격의 가드 이벤트와 매칭 → 사유 라벨 + 등락률.
            kind, guard = bucket.pop(0)
            reason = _GUARD_REASON_KO[kind]
            pct_part = _guard_pct_part(kind, guard)
            _add(
                rec.get("timestamp"),
                kind,
                f"{reason} {label} {qty}주{price_part}{pct_part}{sim_part}",
            )
        else:
            # 가드 없는 일반 매도(시그널·수동 청산 등).
            _add(rec.get("timestamp"), "sell", f"매도 {label} {qty}주{price_part}{sim_part}")

    for rec in events.orders_simulated:
        if str(rec.get("side", "")) == "sell":
            _add_sell(rec, simulated=True)
    for rec in events.orders_accepted:
        if str(rec.get("side", "")) == "sell":
            _add_sell(rec, simulated=False)

    # EOD 강제청산 체결 — orders_* 와 이벤트명이 달라 매도와 겹치지 않는다.
    def _add_liq_fill(rec: dict[str, Any], *, simulated: bool) -> None:
        symbol = str(rec.get("symbol", ""))
        qty = _as_int(rec.get("qty")) or 0
        if not symbol or qty <= 0:
            return
        price = _as_float(rec.get("sell_price"))
        label = _with_name_label(symbol, names)
        price_part = f" @{_format_won_int(price)}" if price is not None else ""
        sim_part = " (시뮬)" if simulated else ""
        _add(rec.get("timestamp"), "liquidate", f"청산 체결 {label} {qty}주{price_part}{sim_part}")

    for rec in events.liquidations_simulated:
        _add_liq_fill(rec, simulated=True)
    for rec in events.liquidations_accepted:
        _add_liq_fill(rec, simulated=False)

    # 체결 로그가 따로 안 남은 가드 이벤트만 폴백으로 emit(가드의 qty/price 사용).
    # partial_take 만 qty_to_sell 을 들고 있어 수량 표기 가능 — 손절/트레일링은 수량 생략.
    for (symbol, price), bucket in guard_buckets.items():
        label = _with_name_label(symbol, names)
        price_part = f" @{_format_won_int(price)}" if price is not None else ""
        for kind, guard in bucket:
            qty = _as_int(guard.get("qty_to_sell")) or 0 if kind == "partial_take" else 0
            qty_part = f" {qty}주" if qty else ""
            reason = _GUARD_REASON_KO[kind]
            pct_part = _guard_pct_part(kind, guard)
            _add(guard.get("timestamp"), kind, f"{reason} {label}{qty_part}{price_part}{pct_part}")

    for rec in events.kill_switches:
        change = _as_float(rec.get("today_change"))
        limit = _as_float(rec.get("daily_loss_limit"))
        change_pct = f"{change * 100:+.2f}%" if change is not None else "?"
        limit_pct = f"-{limit * 100:.1f}%" if limit is not None else "?"
        _add(
            rec.get("timestamp"),
            "kill_switch",
            f"🛑 일일 손실 한도 도달 (당일 {change_pct}, 한도 {limit_pct}) — 강제 청산",
        )

    for rec in events.liquidate_starts:
        reason = str(rec.get("reason", ""))
        reason_ko = _LIQUIDATE_REASON_KO.get(reason, reason or "청산")
        symbols_raw = rec.get("symbols") or []
        symbols = [str(s) for s in symbols_raw if isinstance(s, str)]
        if symbols:
            shown = ", ".join(_with_name_label(s, names) for s in symbols[:3])
            tail = f" 외 {len(symbols) - 3}" if len(symbols) > 3 else ""
            text = f"청산 시작 ({reason_ko}): {shown}{tail}"
        else:
            text = f"청산 시작 ({reason_ko}) — 보유 없음"
        _add(rec.get("timestamp"), "liquidate", text)

    # raw timestamp 문자열 정렬 = ISO 8601 사전식 정렬 = 시각 정렬.
    items.sort(key=lambda pair: pair[0])
    return [ev for _, ev in items]
