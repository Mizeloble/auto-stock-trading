"""사람용 텍스트 렌더링 — 일일 요약 메모(note_ko)와 종목명 라벨 헬퍼.

빌더가 채우는 한국어 요약 한 줄과, 외부(notify·news)에서도 쓰는 코드→종목명
라벨 헬퍼를 모은다.
"""

from __future__ import annotations

from collections.abc import Iterable

from .models import DailyReport, LiquidationRecord, TradeRecord


def _summary_ko(
    *,
    chosen: list[str],
    buys: list[TradeRecord],
    sells: list[TradeRecord],
    blocked: list[tuple[str, str]],
    liquidations: list[LiquidationRecord],
    realized_pnl: float | None,
    realized_pnl_after_cost: float | None,
    error_count: int,
) -> str:
    """note_ko 빌더. 2026-05-11 회귀: 발굴(`chosen`)과 실제 체결(`buys`) 을 구분.

    - 기존 동작: `매수 N건` 의 N 을 `len(chosen)` 으로 표기 → kill switch / 게이트로 매수
      실패한 날에도 "매수 3건" 으로 찍혀 사용자·주간 인사이트 LLM 둘 다 오독했음.
    - 새 동작: `매수 N건` 의 N 은 실제 체결된 `buys` 기준. 발굴은 됐는데 체결 0건이면
      "발굴 N건 (체결 0)" 로 분리 표기 → buys 배열이 비어있다는 사실을 메모와 일치시킴.
    """
    if not chosen and not liquidations and not buys and not sells:
        if blocked:
            return f"매수 후보 {len(blocked)}건 모두 게이트 차단 — 거래 없음."
        return "거래 없음."
    parts: list[str] = []
    if buys:
        # 중복 종목 제거(같은 종목 분할 매수도 1건으로 — 발굴 카운트와 일관성).
        seen: set[str] = set()
        buy_syms: list[str] = []
        for trade in buys:
            if trade.symbol not in seen:
                seen.add(trade.symbol)
                buy_syms.append(trade.symbol)
        parts.append(
            f"매수 {len(buy_syms)}건 ({', '.join(buy_syms[:3])}{'…' if len(buy_syms) > 3 else ''})"
        )
    elif chosen:
        # 발굴은 됐지만 실제 매수 0건 — 메모와 buys 배열의 정합성 유지.
        parts.append(f"발굴 {len(chosen)}건 (체결 0)")
    if sells:
        parts.append(f"매도 {len(sells)}건")
    if liquidations:
        parts.append(f"청산 {len(liquidations)}건")
    if realized_pnl is not None:
        sign = "+" if realized_pnl >= 0 else ""
        pnl_part = f"실현 {sign}{int(realized_pnl):,}원"
        # 비용 차감치도 같이 보여줌 — 실거래 수익 가시화.
        if realized_pnl_after_cost is not None and realized_pnl_after_cost != realized_pnl:
            sign_c = "+" if realized_pnl_after_cost >= 0 else ""
            pnl_part += f" (비용 차감 {sign_c}{int(realized_pnl_after_cost):,}원)"
        parts.append(pnl_part)
    if error_count > 0:
        parts.append(f"에러 {error_count}건")
    return " · ".join(parts)


def merge_symbol_names(reports: Iterable[DailyReport]) -> dict[str, str]:
    """여러 DailyReport 의 discovery_chosen_names 를 하나로 병합 → 코드→종목명.

    주간 단계(인사이트·프로파일 제안)가 LLM 입력/출력에서 종목을 코드만 보던 문제를
    고치기 위한 공용 헬퍼. 같은 코드가 여러 날 다른 이름이면 마지막(최신)이 우선.
    """
    out: dict[str, str] = {}
    for r in reports:
        for sym, name in r.discovery_chosen_names.items():
            if sym and isinstance(name, str) and name.strip():
                out[sym] = name.strip()
    return out


def with_symbol_name(symbol: str, name: str | None) -> str:
    """'005930 삼성전자' 형식 — 이름이 없으면 코드만 (notify/state·telegram 와 동일 폴백)."""
    if name and name.strip():
        return f"{symbol} {name.strip()}"
    return symbol
