"""paper-day 풀 사이클 통합 테스트.

29단계 PR7 (#22).

PR1~PR6 의 통합 검증 — 키움 REST 응답을 respx 로 mock 하고, 실제
PaperRunner / RiskManager / OrderJournal / classify_reject 가 함께 돌아가는
시나리오. 단위 테스트가 잡지 못하는 *컴포넌트 사이의 흐름* 을 검증한다.

기존 `tests/integration/` 은 실 키움 API 호출용 (모의투자 키 필요). 이 파일은
*매번 도는* 테스트 — KIWOOM_APP_KEY 의존 X, mock 으로만 동작.

검증 시나리오:
1. 정상 매수: 시그널 → risk 통과 → mock 주문 송신 → mock 응답 → 로컬 포지션
   갱신 + journal 기록 + notifier 알림.
2. 거절 + skip: 키움이 상한가 거절 → classify_reject → _skipped_until 등록 →
   다음 동일 종목 시그널은 차단 (place_order 미호출).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from auto_trading._common import KST, Bar, Side
from auto_trading.account.order_journal import OrderJournal
from auto_trading.config import KiwoomMode, Settings
from auto_trading.kiwoom.endpoints import PATH_ORDER, PATH_TOKEN
from auto_trading.kiwoom.rest import KiwoomRestClient
from auto_trading.risk.manager import AccountState
from auto_trading.runner import PaperRunner
from auto_trading.strategy.base import Bar as StratBar
from auto_trading.strategy.base import Signal, Strategy


class _AlwaysBuy(Strategy):
    """매 봉마다 BUY 시그널 1건 — 통합 테스트용."""

    name = "always_buy"

    def on_bar(self, bar: StratBar) -> list[Signal]:
        return [
            Signal(
                symbol=bar.symbol,
                side=Side.BUY,
                weight=0.05,  # 작게 — RiskManager 의 max_position_weight 0.20 안에 들어가게
                reason="integration_test",
                ts=bar.ts,
                price_hint=Decimal(str(bar.close)),
            )
        ]


def _settings() -> Settings:
    return Settings(
        kiwoom_mode=KiwoomMode.MOCK,
        kiwoom_app_key="k",
        kiwoom_app_secret="s",
    )


def _bar(symbol: str = "005930", close: float = 70_000.0) -> Bar:
    return Bar(
        symbol=symbol,
        ts=datetime.now(KST),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1_000,
    )


def _account() -> AccountState:
    return AccountState(
        deposit=10_000_000,
        total_equity=10_000_000,
        positions={},
        avg_prices={},
        day_start_equity=10_000_000,
    )


def _token_resp() -> httpx.Response:
    exp = (datetime.now() + timedelta(hours=24)).strftime("%Y%m%d%H%M%S")
    return httpx.Response(200, json={"token": "tkn", "expires_dt": exp})


# ---- Scenario 1: 정상 매수 풀 사이클 -----------------------------------------


async def test_full_cycle_buy_signal_to_position_journal_and_notifier(tmp_path: Path) -> None:
    """BUY 시그널 → mock 주문 → 200 OK → 포지션 갱신 + 저널 + 알림."""
    settings = _settings()
    journal = OrderJournal(path=tmp_path / "state.jsonl")
    runner = PaperRunner(
        strategy=_AlwaysBuy(),
        symbols=["005930"],
        journal=journal,
    )
    fake_notifier = AsyncMock()
    runner.notifier = fake_notifier
    account = _account()
    bar = _bar()

    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        order_route = mock.post(PATH_ORDER).mock(
            return_value=httpx.Response(
                200, json={"return_code": 0, "return_msg": "정상", "ord_no": "ORD-1"}
            )
        )
        async with KiwoomRestClient(settings=settings) as client:
            await runner._on_bar(rest=client, bar=bar, account=account)

    # 1. 키움에 주문이 송신됐다.
    assert order_route.called
    body = order_route.calls[0].request.read().decode()
    assert '"stk_cd":"005930"' in body
    # 2. 로컬 포지션이 즉시 반영됐다 (다음 잔고 snapshot 까지 갭 메움).
    assert account.positions.get("005930", 0) > 0
    assert "005930" in account.avg_prices
    # 3. 저널에 submit + accepted 가 기록됐다.
    events = list(journal.replay())
    kinds = [e["event"] for e in events]
    assert "submit" in kinds
    assert "accepted" in kinds
    accepted = next(e for e in events if e["event"] == "accepted")
    assert accepted["order_no"] == "ORD-1"
    # 4. 알림이 발송됐다 (signal_logged + order_accepted 등 여러 종류).
    notify_events = [c.args[0] for c in fake_notifier.notify.call_args_list]
    assert "runner.order_accepted" in notify_events
    # skip 등록은 안 됐다.
    assert "005930" not in runner._skipped_until


# ---- Scenario 2: 키움 거절(상한가) → skip → 다음 시그널 차단 -----------------


async def test_full_cycle_kiwoom_rejection_blocks_next_signal(tmp_path: Path) -> None:
    """상한가 거절 → _skipped_until 등록 → 다음 BUY 시그널은 place_order 송신 X."""
    settings = _settings()
    journal = OrderJournal(path=tmp_path / "state.jsonl")
    runner = PaperRunner(
        strategy=_AlwaysBuy(),
        symbols=["005930"],
        journal=journal,
    )
    runner.notifier = AsyncMock()
    account = _account()

    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        # 모든 주문 시도에 상한가 거절 응답.
        order_route = mock.post(PATH_ORDER).mock(
            return_value=httpx.Response(
                200,
                json={"return_code": 1, "return_msg": "상한가에 도달하여 주문 불가"},
            )
        )
        async with KiwoomRestClient(settings=settings) as client:
            # 첫 번째 봉 → 시그널 → 주문 → 거절 → skip 등록.
            await runner._on_bar(rest=client, bar=_bar(close=70_000.0), account=account)
            # 두 번째 봉 → 시그널 → skip 검사 차단 (주문 송신 X).
            await runner._on_bar(rest=client, bar=_bar(close=70_500.0), account=account)

    # 주문은 *한 번만* 송신됐다 — 두 번째 시그널은 skip 으로 차단.
    assert order_route.call_count == 1
    # _skipped_until 에 등록 (상한가는 600초 skip).
    assert "005930" in runner._skipped_until
    # 저널에는 submit + failed 라인 (accepted 없음).
    events = list(journal.replay())
    kinds = [e["event"] for e in events]
    assert kinds.count("submit") == 1
    assert "failed" in kinds
    assert "accepted" not in kinds


# ---- Scenario 3: 일반 예외(네트워크) → category=UNKNOWN, skip 0 -------------


async def test_full_cycle_network_error_does_not_register_skip(tmp_path: Path) -> None:
    """네트워크 에러는 KiwoomError 가 아니라 일반 예외 — skip 등록 X."""
    settings = _settings()
    journal = OrderJournal(path=tmp_path / "state.jsonl")
    runner = PaperRunner(
        strategy=_AlwaysBuy(),
        symbols=["005930"],
        journal=journal,
    )
    fake_notifier = AsyncMock()
    runner.notifier = fake_notifier
    account = _account()

    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        # 키움 응답 자체가 실패 (5xx).
        mock.post(PATH_ORDER).mock(return_value=httpx.Response(503, json={}))
        async with KiwoomRestClient(settings=settings) as client:
            await runner._on_bar(rest=client, bar=_bar(), account=account)

    # 일반 예외 → skip 등록 X.
    assert "005930" not in runner._skipped_until
    # 알림은 category=unknown 으로.
    notify_calls = [
        c for c in fake_notifier.notify.call_args_list if c.args[0] == "runner.order_failed"
    ]
    assert notify_calls, "runner.order_failed 알림이 발송되어야 함"
    _, kwargs = notify_calls[0]
    assert kwargs.get("category") == "unknown"
    assert kwargs.get("skip_seconds") == 0


pytestmark = pytest.mark.asyncio
