"""market.quote 회귀 테스트.

키움 mock 서버 실측으로 확인 — TR `ka10001` 가 path `/api/dostk/stkinfo` 에서
`cur_prc`(부호 포함, 예: "-219500") / `pred_pre` / `flu_rt` / `trde_qty` 를 반환한다.
quote.py 는 한때 호가 TR(`ka10004`+`PATH_MARKET`) 로 호출해 응답에 `cur_prc` 가
없었고 결과가 모두 0 으로 떨어졌다 — 같은 류 회귀를 즉시 잡도록 mock 으로 재현한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import respx

from auto_trading.config import KiwoomMode, Settings
from auto_trading.kiwoom.endpoints import PATH_STKINFO, PATH_TOKEN, TR
from auto_trading.kiwoom.rest import KiwoomRestClient
from auto_trading.kiwoom.schemas import KiwoomError
from auto_trading.market.quote import get_quote


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


async def test_get_quote_uses_stkinfo_path_and_parses_fields(settings: Settings) -> None:
    # mock 서버에서 본 실측 형태 — 가격에 부호가 붙어 있고 (음수=하락) price 는 절댓값으로 변환돼야 함
    payload = {
        "return_code": 0,
        "return_msg": "정상적으로 처리되었습니다",
        "stk_cd": "005930",
        "stk_nm": "삼성전자",
        "cur_prc": "-219500",
        "pred_pre": "-5000",
        "flu_rt": "-2.23",
        "trde_qty": "19626666",
    }
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        quote_route = mock.post(PATH_STKINFO).mock(return_value=httpx.Response(200, json=payload))

        async with KiwoomRestClient(settings=settings) as client:
            q = await get_quote(client, "005930")

    assert quote_route.called
    sent = quote_route.calls[0].request
    assert sent.headers["api-id"] == TR.STOCK_BASIC_INFO  # ka10001 - 호가 TR 로 되돌리면 즉시 실패
    assert q.symbol == "005930"
    assert q.price == Decimal("219500")  # 부호 제거 후 절댓값
    assert q.change == Decimal("-5000")
    assert q.change_pct == Decimal("-2.23")
    assert q.volume == 19626666


async def test_get_quote_raises_on_unsupported_uri(settings: Settings) -> None:
    """경로/TR 불일치를 키움이 거부할 때 KiwoomError 로 surface 되는지."""
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        mock.post(PATH_STKINFO).mock(
            return_value=httpx.Response(
                200,
                json={
                    "return_code": 9,
                    "return_msg": "해당 URI에서는 지원하는 API ID가 아닙니다",
                },
            )
        )
        async with KiwoomRestClient(settings=settings) as client:
            with pytest.raises(KiwoomError) as exc:
                await get_quote(client, "005930")

    assert exc.value.code == "9"
    assert "지원하는 API ID" in exc.value.message
