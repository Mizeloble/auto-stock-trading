"""account.balance 회귀 — 종목 코드 정규화."""

from __future__ import annotations

import httpx
import respx

from auto_trading.account.balance import get_balance
from auto_trading.config import Settings
from auto_trading.kiwoom.endpoints import PATH_ACCOUNT
from auto_trading.kiwoom.rest import KiwoomRestClient


def _token_resp() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "return_code": 0,
            "token": "abc",
            "expires_dt": "20991231235959",
        },
    )


def _settings() -> Settings:
    return Settings(
        kiwoom_app_key="k",
        kiwoom_app_secret="s",
        kiwoom_account_no="acct",
    )


async def test_holdings_strip_a_prefix() -> None:
    """잔고 응답의 'A005930' / '252670_AL' 같은 변종이 6자리로 정규화됨.

    회귀: 2026-05-07 paper-day 운영에서 'A005930' 이 그대로 account.positions
    에 들어가 청산 시 'A005930' 으로 송신 → 키움 1902 거절.
    """
    settings = _settings()
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post("/oauth2/token").mock(return_value=_token_resp())
        mock.post(PATH_ACCOUNT).mock(
            return_value=httpx.Response(
                200,
                json={
                    "return_code": 0,
                    "entr": "50000000",
                    "tot_evlt_amt": "53000000",
                    "tot_evltv_prft": "0",
                    "stk_acnt_evlt_prst": [
                        {
                            "stk_cd": "A005930",
                            "stk_nm": "삼성전자",
                            "rmnd_qty": "12",
                            "pur_pric": "275500",
                            "cur_prc": "270500",
                            "evlt_amt": "3246000",
                            "evltv_prft": "-89422",
                            "prft_rt": "-2.70",
                        },
                        {
                            "stk_cd": "252670_AL",
                            "stk_nm": "KODEX 인버스",
                            "rmnd_qty": "5",
                            "pur_pric": "10000",
                            "cur_prc": "9800",
                            "evlt_amt": "49000",
                            "evltv_prft": "-1000",
                            "prft_rt": "-2.0",
                        },
                    ],
                },
            )
        )
        async with KiwoomRestClient(settings=settings) as client:
            snap = await get_balance(client)
    assert {h.symbol for h in snap.holdings} == {"005930", "252670"}
    assert all(not h.symbol.startswith("A") for h in snap.holdings)
    assert all("_" not in h.symbol for h in snap.holdings)
