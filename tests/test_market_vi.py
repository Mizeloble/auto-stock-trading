"""market.vi 회귀 테스트.

키움 `ka10054` 모의 응답(`motn_stk` 리스트)을 mock 으로 재현해
(1) 행 → ViStock 매핑 (2) 동적/정적 괴리율 선택 (3) 심볼 정규화
(4) ka10054 필수 파라미터 12개 전송을 잡는다. probe 2026-06-21 실측 스키마 기반.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest
import respx

from auto_trading.config import KiwoomMode, Settings
from auto_trading.kiwoom.endpoints import PATH_STKINFO, PATH_TOKEN, TR
from auto_trading.kiwoom.rest import KiwoomRestClient
from auto_trading.market.vi import vi_triggered


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


def _vi_payload(rows: list[dict]) -> dict:
    return {"return_code": 0, "return_msg": "정상", "motn_stk": rows}


# probe 2026-06-21 실측 행 스키마.
_DYNAMIC_ROW = {
    "stk_cd": "289930",
    "stk_nm": "웨이비스",
    "acc_trde_qty": "402",
    "motn_pric": "10050",
    "dynm_dispty_rt": "-8.47",
    "static_dispty_rt": "0.00",
    "virelis_time": "180230",
    "viaplc_tp": "동적",
    "vimotn_cnt": "2",
    "stex_tp": "KRX",
}
_STATIC_ROW = {
    "stk_cd": "A092600",  # prefix 정규화 검증
    "stk_nm": "앤씨앤",
    "acc_trde_qty": "1",
    "motn_pric": "+3125",  # 부호 정규화
    "dynm_dispty_rt": "0.00",
    "static_dispty_rt": "+9.65",
    "virelis_time": "180216",
    "viaplc_tp": "정적",
    "vimotn_cnt": "1",
    "stex_tp": "KRX",
}


async def test_vi_triggered_parses_rows(settings: Settings) -> None:
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        route = mock.post(PATH_STKINFO).mock(
            return_value=httpx.Response(200, json=_vi_payload([_DYNAMIC_ROW, _STATIC_ROW]))
        )
        async with KiwoomRestClient(settings=settings) as client:
            stocks = await vi_triggered(client, market="ALL")

    # api-id 회귀 게이트
    assert route.calls[0].request.headers["api-id"] == TR.VI_TRIGGERED

    assert len(stocks) == 2
    dyn, sta = stocks

    # 동적 VI → 동적 괴리율 선택, 정적은 무시
    assert dyn.symbol == "289930"
    assert dyn.name == "웨이비스"
    assert dyn.kind == "동적"
    assert dyn.trigger_price == 10050
    assert str(dyn.disparity_pct) == "-8.47"
    assert dyn.release_time == "180230"
    assert dyn.trigger_count == 2
    assert dyn.volume == 402

    # 정적 VI → 정적 괴리율 선택 + 심볼 prefix/부호 정규화
    assert sta.symbol == "092600"  # 'A' prefix 제거
    assert sta.kind == "정적"
    assert sta.trigger_price == 3125  # '+' 제거 + 절댓값
    assert str(sta.disparity_pct) == "9.65"
    assert sta.trigger_count == 1


async def test_vi_triggered_empty_when_no_rows(settings: Settings) -> None:
    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        mock.post(PATH_STKINFO).mock(return_value=httpx.Response(200, json=_vi_payload([])))
        async with KiwoomRestClient(settings=settings) as client:
            stocks = await vi_triggered(client, market="KOSPI")
    assert stocks == []


async def test_vi_triggered_sends_all_required_body_fields(settings: Settings) -> None:
    """ka10054 필수 12필드 회귀 게이트 — probe 2026-06-21 에서 누락이 1511 에러였음.

    하나라도 사라지면 키움이 `1511 필수 입력 값` 으로 거절하므로 이 테스트가 실패해야 함.
    """
    captured: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.append(json.loads(request.content))
        return httpx.Response(200, json=_vi_payload([]))

    async with respx.mock(base_url=settings.rest_base_url) as mock:
        mock.post(PATH_TOKEN).mock(return_value=_token_resp())
        mock.post(PATH_STKINFO).mock(side_effect=_handler)
        async with KiwoomRestClient(settings=settings) as client:
            await vi_triggered(client, market="KOSDAQ")

    assert captured, "POST 가 한 번도 안 갔음"
    body = captured[0]
    required = {
        "mrkt_tp",
        "bf_mkrt_tp",
        "motn_tp",
        "skip_stk",
        "trde_qty_tp",
        "min_trde_qty",
        "max_trde_qty",
        "trde_prica_tp",
        "min_trde_prica",
        "max_trde_prica",
        "motn_drc",
        "stex_tp",
    }
    missing = required - set(body)
    assert not missing, f"필수 필드 누락: {missing} (probe 2026-06-21 회귀)"
    # KOSDAQ → mrkt_tp 101
    assert body["mrkt_tp"] == "101"
