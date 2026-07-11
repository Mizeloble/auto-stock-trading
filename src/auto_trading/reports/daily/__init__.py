"""DailyReport 도메인 + 로그·스냅샷 → 리포트 빌더 + JSON I/O.

원래 단일 `daily.py`(1400여 줄)를 책임별 서브모듈로 분리한 패키지. 외부에서 쓰는
공개 API 경로(`auto_trading.reports.daily.X`)는 이 `__init__` 재노출로 그대로 유지된다.

- `models`    : DailyReport 등 dataclass(순수 데이터)
- `_util`     : 작은 변환·포맷 헬퍼(private)
- `extract`   : 로그·스냅샷 → 매수/매도/청산/게이트/차단 레코드
- `aggregate` : 비용·거래대금·FIFO 실현손익 집계 + COST_* 상수
- `timeline`  : 시계열 한 줄 메시지 빌더
- `render`    : note_ko 요약 + 종목명 라벨 헬퍼
- `persist`   : JSON 저장·로드(직렬화 footgun 주의 — reports/CLAUDE.md)
- `builder`   : build_daily_report 오케스트레이션
"""

from __future__ import annotations

from .aggregate import COST_COMMISSION_RATE, COST_SLIPPAGE_BPS, COST_TAX_RATE_SELL
from .builder import build_daily_report, refresh_snapshot_joins
from .models import (
    BlockedOutcome,
    ClosingHolding,
    DailyReport,
    LiquidationRecord,
    MarketGateInfo,
    RoundTrip,
    TimelineEvent,
    TradeRecord,
)
from .persist import load_daily_report, save_daily_report
from .render import merge_symbol_names, with_symbol_name

__all__ = [
    "COST_COMMISSION_RATE",
    "COST_SLIPPAGE_BPS",
    "COST_TAX_RATE_SELL",
    "BlockedOutcome",
    "ClosingHolding",
    "DailyReport",
    "LiquidationRecord",
    "MarketGateInfo",
    "RoundTrip",
    "TimelineEvent",
    "TradeRecord",
    "build_daily_report",
    "load_daily_report",
    "merge_symbol_names",
    "refresh_snapshot_joins",
    "save_daily_report",
    "with_symbol_name",
]
