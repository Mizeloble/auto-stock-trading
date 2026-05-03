"""거래량 순위 + 업종 일봉 응답 dict 실측 캡처 (1회용 진단 스크립트).

목적:
1. `TR.RANK_VOLUME` (`ka10030`) 가 KOSPI/KOSDAQ 별로 어떤 키로 응답하는지 확인.
2. `TR.SECTOR_INDEX_DAILY` (`ka20009`) 가 KOSPI/KOSDAQ 종합지수 일봉을 돌려주는지 확인.

응답 키가 다르면 `endpoints.py` / `market/rank.py` / `market/index.py` 의 필드명 후보 체인을
실측에 맞춰 보정한다.

사용:
    uv run python scripts/probe_rank_index.py

KIWOOM_APP_KEY / KIWOOM_APP_SECRET 가 환경 또는 ../.env 에 있어야 함.
KIWOOM_MODE=mock 에서만 동작 (실거래 모드에서 진단 호출 차단).
"""

from __future__ import annotations

import asyncio
import datetime
import json
from typing import Any

from auto_trading.config import get_settings
from auto_trading.kiwoom.endpoints import (
    MARKET_TP_KOSDAQ,
    MARKET_TP_KOSPI,
    PATH_RANK,
    PATH_SECTOR,
    SECTOR_CODE_KOSDAQ,
    SECTOR_CODE_KOSPI,
    TR,
)
from auto_trading.kiwoom.rest import KiwoomRestClient


def _redact(d: dict[str, Any]) -> dict[str, Any]:
    """secret 키워드가 들어간 값은 마스킹."""
    redacted: dict[str, Any] = {}
    for k, v in d.items():
        if any(s in k.lower() for s in ("secret", "token", "key")):
            redacted[k] = "***"
        else:
            redacted[k] = v
    return redacted


def _dump(label: str, data: dict[str, Any], rows_preview: int = 5) -> None:
    print(f"\n=== {label} ===")
    # 응답 본체에 list[dict] 가 있으면 앞 몇 개만 미리보기로 자른다 (콘솔 도배 방지)
    trimmed: dict[str, Any] = {}
    for k, v in data.items():
        if isinstance(v, list) and v and isinstance(v[0], dict) and len(v) > rows_preview:
            trimmed[k] = [*v[:rows_preview], {"_more": f"... +{len(v) - rows_preview} rows"}]
        else:
            trimmed[k] = v
    print(json.dumps(_redact(trimmed), ensure_ascii=False, indent=2, default=str))


async def _post_raw(
    client: KiwoomRestClient, tr_id: str, path: str, body: dict[str, Any]
) -> dict[str, Any]:
    """`KiwoomRestClient.request` 는 return_code≠0 이면 raise — raw 본문을 보려면 직접 POST."""
    token = await client.token_provider.get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "api-id": tr_id,
        "cont-yn": "N",
        "next-key": "",
        "Content-Type": "application/json;charset=UTF-8",
    }
    resp = await client._client.post(path, headers=headers, json=body)
    print(f"  HTTP {resp.status_code}  api-id={tr_id}  path={path}")
    try:
        return resp.json()
    except Exception:
        return {"_text": resp.text[:500]}


async def main() -> None:
    settings = get_settings()
    print(f"mode={settings.kiwoom_mode.value}  base={settings.rest_base_url}")
    if not settings.is_mock:
        raise SystemExit("ABORT: KIWOOM_MODE=mock 이 아닙니다. 실거래 모드에서 진단 스크립트 금지.")

    # ka10030 (당일거래량상위) 필수 파라미터 — 2026-05-03 1차 probe 에서 crd_tp 누락이
    # 1511 에러로 surface. market/rank.py 가 보내는 본문과 동일하게.
    rank_body_template = {
        "sort_tp": "1",
        "mang_stk_incls": "0",
        "crd_tp": "0",
        "trde_qty_tp": "0",
        "pric_tp": "0",
        "trde_prica_tp": "0",
        "mrkt_open_tp": "0",
        "stex_tp": "3",
    }
    today_yyyymmdd = datetime.date.today().strftime("%Y%m%d")

    async with KiwoomRestClient() as client:
        # 1) 거래량 상위 KOSPI
        rank_kospi = await _post_raw(
            client,
            TR.RANK_VOLUME,
            PATH_RANK,
            {**rank_body_template, "mrkt_tp": MARKET_TP_KOSPI},
        )
        _dump("RANK_VOLUME · KOSPI", rank_kospi)

        # 2) 거래량 상위 KOSDAQ
        rank_kosdaq = await _post_raw(
            client,
            TR.RANK_VOLUME,
            PATH_RANK,
            {**rank_body_template, "mrkt_tp": MARKET_TP_KOSDAQ},
        )
        _dump("RANK_VOLUME · KOSDAQ", rank_kosdaq)

        # 3) KOSPI 종합지수 일봉 (ka20009 — 2026-05-03 ka20006 가 URI 미지원이라 교체)
        idx_kospi = await _post_raw(
            client,
            TR.SECTOR_INDEX_DAILY,
            PATH_SECTOR,
            {"inds_cd": SECTOR_CODE_KOSPI, "base_dt": today_yyyymmdd},
        )
        _dump("SECTOR_INDEX_DAILY · KOSPI 001", idx_kospi)

        # 4) KOSDAQ 종합지수 일봉. 1차 probe 에서 분당 호출 제한(429) 걸림 — 2초 대기.
        await asyncio.sleep(2)
        idx_kosdaq = await _post_raw(
            client,
            TR.SECTOR_INDEX_DAILY,
            PATH_SECTOR,
            {"inds_cd": SECTOR_CODE_KOSDAQ, "base_dt": today_yyyymmdd},
        )
        _dump("SECTOR_INDEX_DAILY · KOSDAQ 101", idx_kosdaq)

    print("\n--- 확인 포인트 ---")
    print("· 응답 본체에 list[dict] 필드가 있는가? (있으면 거기 첫 행의 키들이 우리가 매핑할 후보)")
    print("· 거래량 순위: stk_cd / trde_qty / cur_prc / stk_nm 키가 실제로 있는지")
    print("· 업종 일봉: dt / cur_prc(또는 inds_prc) 키가 실제로 있는지")
    print("· return_code 가 0 이 아니면 → endpoints.py 의 TR/PATH 또는 mrkt_tp/inds_cd 보정 필요")


if __name__ == "__main__":
    asyncio.run(main())
