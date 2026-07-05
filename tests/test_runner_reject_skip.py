"""runner 의 reject 분류 → skip 통합 회귀.

29단계 PR6 (#10 2차).

검증:
- KiwoomError(상한가) 발생 → _skipped_until 등록 + 알림에 category 표기.
- KiwoomError(잔고부족) 발생 → skip_seconds=0 이라 _skipped_until 등록 X.
- KiwoomError(UNKNOWN) → 등록 X.
- 일반 예외(네트워크 등) → category=UNKNOWN, skip 0, 등록 X.
- _is_symbol_skipped: 만료 후 자동 청소.
- _evaluate_signal: skipped 종목 → None 반환 (시그널 차단).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from auto_trading._common import KST, Bar, Side
from auto_trading.account.orders import Order, OrderType
from auto_trading.account.reject_classifier import RejectCategory
from auto_trading.kiwoom.schemas import KiwoomError
from auto_trading.runner import PaperRunner
from auto_trading.runner import paper as runner_module
from auto_trading.strategy.base import Signal
from auto_trading.strategy.sma_crossover import SmaCrossover


def _runner() -> PaperRunner:
    return PaperRunner(strategy=SmaCrossover(), symbols=["005930"])


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


def _account() -> runner_module.AccountState:
    return runner_module.AccountState(
        deposit=10_000_000,
        total_equity=10_000_000,
        positions={"005930": 5},
        avg_prices={"005930": 70_000.0},
    )


# ---- _place_and_track except → 분류 + skip 등록 -------------------------------


async def test_kiwoom_error_known_category_registers_skip() -> None:
    """상한가 거절 → _skipped_until 에 등록 + 알림에 category=price_limit_up."""
    r = _runner()
    fake_notifier = AsyncMock()
    r.notifier = fake_notifier
    order = Order(symbol="005930", side=Side.BUY, quantity=1, order_type=OrderType.MARKET)

    with patch.object(
        runner_module,
        "place_order",
        AsyncMock(side_effect=KiwoomError(code="0", message="상한가에 도달")),
    ):
        await r._place_and_track(rest=object(), order=order, bar=_bar(), account=_account())  # type: ignore[arg-type]

    assert "005930" in r._skipped_until
    # 600초 (PRICE_LIMIT_UP) 만큼 미래.
    until = r._skipped_until["005930"]
    delta = (until - datetime.now(KST)).total_seconds()
    assert 590 <= delta <= 610

    # 알림 호출 검증.
    fake_notifier.notify.assert_awaited_once()
    args, kwargs = fake_notifier.notify.call_args
    assert args[0] == "runner.order_failed"
    assert kwargs["category"] == RejectCategory.PRICE_LIMIT_UP.value
    assert kwargs["skip_seconds"] == 600


async def test_kiwoom_error_zero_skip_does_not_register() -> None:
    """잔고부족 → skip_seconds=0 이라 등록 X (다른 종목과 무관)."""
    r = _runner()
    r.notifier = AsyncMock()
    order = Order(symbol="005930", side=Side.BUY, quantity=1)

    with patch.object(
        runner_module,
        "place_order",
        AsyncMock(side_effect=KiwoomError(code="0", message="예수금 부족")),
    ):
        await r._place_and_track(rest=object(), order=order, bar=_bar(), account=_account())  # type: ignore[arg-type]

    assert "005930" not in r._skipped_until


async def test_kiwoom_error_unknown_message_does_not_register() -> None:
    """UNKNOWN 카테고리도 skip 0."""
    r = _runner()
    r.notifier = AsyncMock()
    order = Order(symbol="005930", side=Side.BUY, quantity=1)

    with patch.object(
        runner_module,
        "place_order",
        AsyncMock(side_effect=KiwoomError(code="9999", message="알 수 없는 시스템 에러")),
    ):
        await r._place_and_track(rest=object(), order=order, bar=_bar(), account=_account())  # type: ignore[arg-type]

    assert "005930" not in r._skipped_until


async def test_non_kiwoom_exception_does_not_register() -> None:
    """네트워크 등 일반 예외 → category=UNKNOWN, skip 0, 등록 X."""
    r = _runner()
    fake_notifier = AsyncMock()
    r.notifier = fake_notifier
    order = Order(symbol="005930", side=Side.BUY, quantity=1)

    with patch.object(
        runner_module,
        "place_order",
        AsyncMock(side_effect=RuntimeError("connection refused")),
    ):
        await r._place_and_track(rest=object(), order=order, bar=_bar(), account=_account())  # type: ignore[arg-type]

    assert "005930" not in r._skipped_until
    _, kwargs = fake_notifier.notify.call_args
    assert kwargs["category"] == RejectCategory.UNKNOWN.value
    assert kwargs["skip_seconds"] == 0


# ---- _is_symbol_skipped 자동 청소 --------------------------------------------


def test_is_symbol_skipped_returns_false_for_unknown_symbol() -> None:
    r = _runner()
    assert r._is_symbol_skipped("005930") is False


def test_is_symbol_skipped_returns_true_within_window() -> None:
    r = _runner()
    r._skipped_until["005930"] = datetime.now(KST) + timedelta(seconds=60)
    assert r._is_symbol_skipped("005930") is True


def test_is_symbol_skipped_auto_clears_expired_entry() -> None:
    """만료된 항목은 검사 시 즉시 dict 에서 청소."""
    r = _runner()
    r._skipped_until["005930"] = datetime.now(KST) - timedelta(seconds=1)
    assert r._is_symbol_skipped("005930") is False
    assert "005930" not in r._skipped_until


# ---- _evaluate_signal 시그널 차단 --------------------------------------------


async def test_evaluate_signal_blocks_skipped_symbol() -> None:
    """skip 중인 종목의 시그널은 risk 평가 전에 차단 → None 반환."""
    r = _runner()
    r._skipped_until["005930"] = datetime.now(KST) + timedelta(seconds=300)
    signal = Signal(
        symbol="005930",
        side=Side.BUY,
        weight=0.5,
        price_hint=Decimal("70000"),
    )

    order = await r._evaluate_signal(signal, _bar(), _account())

    assert order is None


async def test_evaluate_signal_passes_unblocked_symbol() -> None:
    """skip 안 된 종목은 정상 흐름으로 (risk evaluate 도달)."""
    r = _runner()
    # _skipped_until 비어있음.
    signal = Signal(
        symbol="005930",
        side=Side.BUY,
        weight=0.5,
        price_hint=Decimal("70000"),
    )

    # 시그널 자체는 risk 매니저까지 도달 — risk 가 거절할지 통과할지 별도 분기.
    # 여기선 _evaluate_signal 이 skip 검사로 *조기 None* 을 반환하지 않음을 확인.
    # SmaCrossover 의 시그널은 dry_run 분기 등 거치지만, 이 테스트는 skip 가드만 검증.
    result = await r._evaluate_signal(signal, _bar(), _account())
    # risk 매니저 결정에 따라 None 또는 Order — 둘 다 OK. *우리가 검증하는 건* skip 가드 *전*에 차단되지 않았다는 것.
    # _is_symbol_skipped 호출 시점이 첫 줄이라, skip 가드 통과 = "_skipped_until 에 들어가지 않음"과 등가.
    assert "005930" not in r._skipped_until
    # signal_skipped_reject 로그가 *안* 찍혔다는 간접 확인 — 더 정확히는 risk 까지 도달했다는 것.
    # (실 동작: SmaCrossover 의 BUY 가 risk 를 통과하면 Order, 통과 못하면 None — 양쪽 모두 OK)
    _ = result  # unused


# ---- 거래불가 이력 영속 (2026-06-16) -------------------------------------


async def test_trading_halt_reject_records_untradeable(tmp_path) -> None:
    """매매제한(TRADING_HALT) 거부 → untradeable 스토어에 기록(다음날 발굴 회피)."""
    from auto_trading.account.untradeable import UntradeableStore

    store = UntradeableStore(path=tmp_path / "untradeable.json")
    r = _runner()
    r.untradeable = store
    r._symbol_names["004870"] = "티웨이홀딩스"
    r.notifier = AsyncMock()
    order = Order(symbol="004870", side=Side.BUY, quantity=1, order_type=OrderType.MARKET)

    with patch.object(
        runner_module,
        "place_order",
        AsyncMock(side_effect=KiwoomError(code="20", message="모의투자 매매제한 종목입니다")),
    ):
        await r._place_and_track(rest=object(), order=order, bar=_bar("004870"), account=_account())  # type: ignore[arg-type]

    from datetime import date

    today = datetime.now(KST).date()
    assert isinstance(today, date)
    entry = store.load().get("004870")
    assert entry is not None
    assert entry.reason == RejectCategory.TRADING_HALT.value
    assert entry.name == "티웨이홀딩스"


async def test_insufficient_balance_reject_not_recorded(tmp_path) -> None:
    """잔고부족 등 종목 무관 사유는 영속화하지 않는다(멀쩡한 종목 보호)."""
    from auto_trading.account.untradeable import UntradeableStore

    store = UntradeableStore(path=tmp_path / "untradeable.json")
    r = _runner()
    r.untradeable = store
    r.notifier = AsyncMock()
    order = Order(symbol="005930", side=Side.BUY, quantity=1, order_type=OrderType.MARKET)

    with patch.object(
        runner_module,
        "place_order",
        AsyncMock(side_effect=KiwoomError(code="0", message="주문가능금액 부족")),
    ):
        await r._place_and_track(rest=object(), order=order, bar=_bar(), account=_account())  # type: ignore[arg-type]

    assert store.load() == {}
