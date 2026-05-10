"""모의투자 주문 응답 dict 실측 캡처 (1회용 진단 스크립트).

목적: 키움 모의 서버가 주문 요청에 어떤 키로 주문번호를 돌려주는지 (`ord_no`/`odno`/...)
확인해 `account/orders.py` 의 응답 매핑이 맞는지 검증.

`KiwoomRestClient.request` 는 `return_code≠0` 이면 `KiwoomError` 로 raise 하므로,
raw 본문 자체를 보려면 토큰만 받아 httpx 로 직접 POST 한다.

사용:
    uv run python scripts/diagnostics/probe_orders.py

KIWOOM_APP_KEY / KIWOOM_APP_SECRET 가 환경 또는 ../.env 에 있어야 함.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from auto_trading.config import get_settings
from auto_trading.kiwoom.endpoints import PATH_ORDER, TR
from auto_trading.kiwoom.rest import KiwoomRestClient


def _redact(d: dict[str, Any]) -> dict[str, Any]:
    """secret 키워드가 들어간 값은 마스킹."""
    redacted = {}
    for k, v in d.items():
        if any(s in k.lower() for s in ("secret", "token", "key")):
            redacted[k] = "***"
        else:
            redacted[k] = v
    return redacted


def _dump(label: str, data: dict[str, Any]) -> None:
    print(f"\n=== {label} ===")
    print(json.dumps(_redact(data), ensure_ascii=False, indent=2, default=str))


async def _post_raw(client: KiwoomRestClient, tr_id: str, body: dict[str, Any]) -> dict[str, Any]:
    token = await client.token_provider.get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "api-id": tr_id,
        "cont-yn": "N",
        "next-key": "",
        "Content-Type": "application/json;charset=UTF-8",
    }
    resp = await client._client.post(PATH_ORDER, headers=headers, json=body)
    print(f"  HTTP {resp.status_code}  api-id={tr_id}")
    try:
        return resp.json()
    except Exception:
        return {"_text": resp.text[:500]}


async def main() -> None:
    settings = get_settings()
    print(f"mode={settings.kiwoom_mode.value}  base={settings.rest_base_url}")
    if not settings.is_mock:
        raise SystemExit("ABORT: KIWOOM_MODE=mock 이 아닙니다. 실거래 모드에서 진단 스크립트 금지.")

    async with KiwoomRestClient() as client:
        # 1) 매수 1주 (가격 매우 낮은 지정가). 토요일이면 RC4010 거절, 평일이면 미체결 접수.
        buy_body = {
            "dmst_stex_tp": "KRX",
            "stk_cd": "005930",
            "ord_qty": "1",
            "ord_uv": "10000",
            "trde_tp": "0",
            "cond_uv": "",
        }
        buy_raw = await _post_raw(client, TR.ORDER_BUY, buy_body)
        _dump("BUY raw response", buy_raw)
        for k in ("ord_no", "odno", "ORD_NO"):
            if k in buy_raw:
                print(f"  ↳ buy has key {k!r} = {buy_raw[k]!r}")

        # 2) 매도 시도 (보유 0 가능성 — 거절 응답 키 캡처용)
        sell_body = {
            "dmst_stex_tp": "KRX",
            "stk_cd": "005930",
            "ord_qty": "1",
            "ord_uv": "999999",
            "trde_tp": "0",
            "cond_uv": "",
        }
        sell_raw = await _post_raw(client, TR.ORDER_SELL, sell_body)
        _dump("SELL raw response", sell_raw)

        # 3) 취소 — 가짜 원주문번호로 거절 받아 응답 키 모양 확인
        cancel_body = {
            "dmst_stex_tp": "KRX",
            "orig_ord_no": "0000001",
            "stk_cd": "005930",
            "cncl_qty": "1",
        }
        cancel_raw = await _post_raw(client, TR.ORDER_CANCEL, cancel_body)
        _dump("CANCEL raw response", cancel_raw)


if __name__ == "__main__":
    asyncio.run(main())
