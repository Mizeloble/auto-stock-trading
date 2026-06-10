"""account.orders 회귀 테스트.

목적:
- 매수/매도/취소 요청이 키움 규약대로 path/TR/body 를 보내는지.
- 응답에서 주문번호 키 (`ord_no` 또는 fallback `odno`) 매핑.
- 비즈니스 거절(`return_code≠0`) 이 `KiwoomError` 로 전파되는지.

mock 모의서버 실측 (2026-04-25, 주말):
    {"return_code": 20, "return_msg": "[2000](RC4010:모의투자 영업일이 아닙니다.)"}
→ 성공 응답의 ord_no 키 이름은 평일 장중 재검증 필요(probe_orders.py).
   현재 매핑은 키움 가이드/관행을 따라 ord_no→odno 폴백으로 두되, 회귀는 양쪽 모두 검증한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from auto_trading._common import Side
from auto_trading.account.order_journal import OrderJournal
from auto_trading.account.orders import (
    Order,
    OrderType,
    cancel_order,
    place_order,
)
from auto_trading.config import KiwoomMode, Settings
from auto_trading.kiwoom.endpoints import PATH_ORDER, PATH_TOKEN, TR
from auto_trading.kiwoom.rest import KiwoomRestClient
from auto_trading.kiwoom.schemas import KiwoomError


@pytest.fixture
def settings() -> Settings:
    return Settings(
        kiwoom_mode=KiwoomMode.MOCK,
        kiwoom_app_key="k",
        kiwoom_app_secret="s",
    )


def _token_resp() -> httpx.Response:
    exp = (datetime.now() + timedelta(hours=24)).strftime("%Y%m%d%H%M%S")
    return httpx.Response(200, json={"token": "tkn", "expires_dt": exp})


async def test_buy_market_order_sends_correct_body_and_parses_ord_no(settings: Settings) -> None:
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        route = mock.post(PATH_ORDER).mock(
            return_value=httpx.Response(
                200, json={"return_code": 0, "return_msg": "정상", "ord_no": "0000123"}
            )
        )

        async with KiwoomRestClient(settings=settings) as client:
            res = await place_order(
                client,
                Order(symbol="005930", side=Side.BUY, quantity=10),
            )

    assert route.called
    sent = route.calls[0].request
    assert sent.headers["api-id"] == TR.ORDER_BUY  # kt10000
    body = sent.read().decode()
    assert '"stk_cd":"005930"' in body
    assert '"ord_qty":"10"' in body
    assert '"trde_tp":"3"' in body  # 시장가
    assert '"ord_uv":""' in body  # 시장가는 단가 빈값
    assert '"dmst_stex_tp":"KRX"' in body
    assert res.order_no == "0000123"
    assert res.raw["return_code"] == 0


async def test_sell_limit_order_sends_price_and_uses_sell_tr(settings: Settings) -> None:
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        route = mock.post(PATH_ORDER).mock(
            return_value=httpx.Response(200, json={"return_code": 0, "ord_no": "0000456"})
        )

        async with KiwoomRestClient(settings=settings) as client:
            res = await place_order(
                client,
                Order(
                    symbol="005930",
                    side=Side.SELL,
                    quantity=2,
                    order_type=OrderType.LIMIT,
                    price=Decimal("75000"),
                ),
            )

    sent = route.calls[0].request
    assert sent.headers["api-id"] == TR.ORDER_SELL  # kt10001
    body = sent.read().decode()
    assert '"trde_tp":"0"' in body  # 지정가
    assert '"ord_uv":"75000"' in body
    assert res.order_no == "0000456"


async def test_place_order_falls_back_to_odno_when_ord_no_missing(settings: Settings) -> None:
    """응답이 `odno` 키만 갖는 변종에 대비한 폴백 (실측 미확인)."""
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        mock.post(PATH_ORDER).mock(
            return_value=httpx.Response(200, json={"return_code": 0, "odno": "ODNO-1"})
        )
        async with KiwoomRestClient(settings=settings) as client:
            res = await place_order(
                client,
                Order(symbol="005930", side=Side.BUY, quantity=1),
            )

    assert res.order_no == "ODNO-1"


async def test_place_order_returns_empty_order_no_when_neither_key_present(
    settings: Settings,
) -> None:
    """주문번호 키가 모두 빠진 응답이라도 예외 없이 빈 문자열로 떨어져야 한다."""
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        mock.post(PATH_ORDER).mock(return_value=httpx.Response(200, json={"return_code": 0}))
        async with KiwoomRestClient(settings=settings) as client:
            res = await place_order(
                client,
                Order(symbol="005930", side=Side.BUY, quantity=1),
            )

    assert res.order_no == ""


async def test_place_order_business_rejection_raises_kiwoom_error(settings: Settings) -> None:
    """모의서버 실측 형태 — 영업일 아님 거절(RC4010) 이 KiwoomError 로 매핑된다."""
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        mock.post(PATH_ORDER).mock(
            return_value=httpx.Response(
                200,
                json={
                    "return_code": 20,
                    "return_msg": "[2000](RC4010:모의투자 영업일이 아닙니다.)",
                },
            )
        )
        async with KiwoomRestClient(settings=settings) as client:
            with pytest.raises(KiwoomError) as exc:
                await place_order(
                    client,
                    Order(symbol="005930", side=Side.BUY, quantity=1),
                )

    assert exc.value.code == "20"
    assert "RC4010" in exc.value.message


async def test_cancel_order_sends_cancel_tr_and_parses_response(settings: Settings) -> None:
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        route = mock.post(PATH_ORDER).mock(
            return_value=httpx.Response(200, json={"return_code": 0, "ord_no": "CANCEL-1"})
        )
        async with KiwoomRestClient(settings=settings) as client:
            res = await cancel_order(
                client,
                original_order_no="0000123",
                symbol="005930",
                quantity=1,
            )

    sent = route.calls[0].request
    assert sent.headers["api-id"] == TR.ORDER_CANCEL  # kt10003
    body = sent.read().decode()
    assert '"orig_ord_no":"0000123"' in body
    assert '"cncl_qty":"1"' in body
    assert res.order_no == "CANCEL-1"


# ---- 2026-05-08 회귀 — 청산 거절 (A prefix) -----------------------------------


async def test_place_order_strips_a_prefix_before_sending(settings: Settings) -> None:
    """잔고/feed 정규화가 빠진 경로에서 'A005930' 이 들어와도 송신은 '005930' 으로.

    회귀: 2026-05-07 paper-day 운영에서 잔고 응답의 'A005930' 이 그대로
    `account.positions` 에 들어가 liquidate_all 이 SELL 'A005930' 송신 →
    키움 1902 거절. orders.place_order 의 송신 직전 normalize 가 1차 방어선.
    """
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        route = mock.post(PATH_ORDER).mock(
            return_value=httpx.Response(200, json={"return_code": 0, "ord_no": "X1"})
        )
        async with KiwoomRestClient(settings=settings) as client:
            await place_order(
                client,
                Order(symbol="A005930", side=Side.SELL, quantity=12),
            )
    body = route.calls[0].request.read().decode()
    assert '"stk_cd":"005930"' in body
    assert "A005930" not in body


# ---- 29단계 PR1 — client UUID + journal --------------------------------------


async def test_place_order_assigns_client_id_when_unspecified(settings: Settings) -> None:
    """journal 미사용 시에도 client_id 가 자동 부여되어 OrderResult 에 실린다."""
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        mock.post(PATH_ORDER).mock(
            return_value=httpx.Response(200, json={"return_code": 0, "ord_no": "OK1"})
        )
        async with KiwoomRestClient(settings=settings) as client:
            res = await place_order(client, Order(symbol="005930", side=Side.BUY, quantity=1))

    assert len(res.client_id) == 32
    int(res.client_id, 16)  # hex 검증


async def test_place_order_respects_caller_supplied_client_id(settings: Settings) -> None:
    """호출자가 지정한 client_id 가 그대로 OrderResult 에 실린다 (재시도 시 동일 UUID)."""
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        mock.post(PATH_ORDER).mock(
            return_value=httpx.Response(200, json={"return_code": 0, "ord_no": "OK"})
        )
        async with KiwoomRestClient(settings=settings) as client:
            res = await place_order(
                client,
                Order(symbol="005930", side=Side.BUY, quantity=1),
                client_id="fixed-cid",
            )

    assert res.client_id == "fixed-cid"


async def test_place_order_journal_records_submit_then_accepted(
    settings: Settings, tmp_path: Path
) -> None:
    journal = OrderJournal(path=tmp_path / "state.jsonl")
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        mock.post(PATH_ORDER).mock(
            return_value=httpx.Response(200, json={"return_code": 0, "ord_no": "ORDX"})
        )
        async with KiwoomRestClient(settings=settings) as client:
            res = await place_order(
                client,
                Order(symbol="005930", side=Side.BUY, quantity=3),
                journal=journal,
                client_id="cid-acc",
            )

    events = list(journal.replay())
    assert [e["event"] for e in events] == ["submit", "accepted"]
    assert all(e["client_id"] == "cid-acc" for e in events)
    assert events[1]["order_no"] == "ORDX"
    assert res.order_no == "ORDX"


async def test_place_order_journal_records_failed_when_kiwoom_rejects(
    settings: Settings, tmp_path: Path
) -> None:
    """비즈니스 거절(KiwoomError) 시 submit + failed 기록 후 예외 전파."""
    journal = OrderJournal(path=tmp_path / "state.jsonl")
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        mock.post(PATH_ORDER).mock(
            return_value=httpx.Response(
                200, json={"return_code": 20, "return_msg": "RC4010 모의투자 영업일 아님"}
            )
        )
        async with KiwoomRestClient(settings=settings) as client:
            with pytest.raises(KiwoomError):
                await place_order(
                    client,
                    Order(symbol="005930", side=Side.BUY, quantity=1),
                    journal=journal,
                    client_id="cid-fail",
                )

    events = list(journal.replay())
    assert [e["event"] for e in events] == ["submit", "failed"]
    assert all(e["client_id"] == "cid-fail" for e in events)
    assert "RC4010" in events[1]["error"]


# ---- 2026-06-10 제3자 검토 — 결과 미확정 주문은 failed 가 아닌 unknown ----------


async def test_place_order_timeout_journals_unknown_and_stays_suspect(
    settings: Settings, tmp_path: Path
) -> None:
    """타임아웃은 키움이 접수했을 수 있다 — unknown 기록 + 복구 스캔에 suspect 로 남는다."""
    from auto_trading.account.recovery import find_suspect_orders

    journal = OrderJournal(path=tmp_path / "state.jsonl")
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        mock.post(PATH_ORDER).mock(side_effect=httpx.ConnectTimeout("timed out"))
        async with KiwoomRestClient(settings=settings) as client:
            with pytest.raises(httpx.ConnectTimeout):
                await place_order(
                    client,
                    Order(symbol="005930", side=Side.BUY, quantity=1),
                    journal=journal,
                    client_id="cid-timeout",
                )

    events = list(journal.replay())
    assert [e["event"] for e in events] == ["submit", "unknown"]
    suspects = find_suspect_orders(events)
    assert [s.client_id for s in suspects] == ["cid-timeout"]


async def test_place_order_5xx_journals_unknown_and_does_not_resend(
    settings: Settings, tmp_path: Path
) -> None:
    """주문의 5xx 는 자동 재전송 없이 1회 송신 + unknown 기록 (이중 체결 방지)."""
    from auto_trading.account.recovery import find_suspect_orders

    journal = OrderJournal(path=tmp_path / "state.jsonl")
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        route = mock.post(PATH_ORDER).mock(return_value=httpx.Response(503, text="unavailable"))
        async with KiwoomRestClient(settings=settings, max_retries=3) as client:
            with pytest.raises(KiwoomError):
                await place_order(
                    client,
                    Order(symbol="005930", side=Side.SELL, quantity=2),
                    journal=journal,
                    client_id="cid-5xx",
                )

    assert route.call_count == 1  # 재전송 없음
    events = list(journal.replay())
    assert [e["event"] for e in events] == ["submit", "unknown"]
    assert [s.client_id for s in find_suspect_orders(events)] == ["cid-5xx"]
