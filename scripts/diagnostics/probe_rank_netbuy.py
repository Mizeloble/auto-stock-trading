"""외인/기관 순매수 계열 순위 TR 실측 캡처 (1회용 진단 스크립트).

목적:
    발굴 신호를 거래량 순위(ka10030) 하나에서 늘리기 위한 선행 조사.
    "거래량과 풀 겹침이 낮은 축" 후보 4종이 mock 에서 실제로 응답하는지, 응답한다면
    필수 파라미터와 행(row) 필드명이 무엇인지 실측한다.

    후보 (키움 순위정보 `rkinfo` 계열):
      1. ka90009  외국인기관매매상위    — 외인·기관 순매수 금액 상위
      2. ka10065  장중투자자별매매상위  — 장중 투자자별(외국인/기관계) 순매수 상위
      3. ka10035  외인연속순매매상위    — 외인 연속 순매수일 상위
      4. ka10034  외인기간별매매상위    — 기간(당일/5일 등) 외인 순매수 상위

    바디는 문서 기준 best-guess 라 거절될 수 있다 — `return_code`/`return_msg` 를
    그대로 찍어서 어떤 필드가 필수인지(1511) 드러내는 것이 이 스크립트의 역할.

사용:
    uv run python scripts/diagnostics/probe_rank_netbuy.py

KIWOOM_APP_KEY / KIWOOM_APP_SECRET 가 환경 또는 .env 에 있어야 함.
KIWOOM_MODE=mock 에서만 동작 (실거래 모드에서 진단 호출 차단).
"""

from __future__ import annotations

import asyncio
import datetime
import json
from typing import Any

from auto_trading.config import get_settings
from auto_trading.kiwoom.endpoints import (
    MARKET_TP_ALL,
    MARKET_TP_KOSPI,
    PATH_RANK,
)
from auto_trading.kiwoom.rest import KiwoomRestClient

# 키움 모의 분당 호출 제한 회피 — 모든 요청 사이에 둠.
SLEEP_BETWEEN_CALLS_SEC = 2.0


def _redact(d: dict[str, Any]) -> dict[str, Any]:
    """secret 키워드가 들어간 값은 마스킹."""
    redacted: dict[str, Any] = {}
    for k, v in d.items():
        if any(s in k.lower() for s in ("secret", "token", "key")):
            redacted[k] = "***"
        else:
            redacted[k] = v
    return redacted


def _dump(label: str, body: dict[str, Any], data: dict[str, Any], rows_preview: int = 3) -> None:
    print(f"\n=== {label} ===")
    print(f"  body={json.dumps(body, ensure_ascii=False)}")
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
        return dict(resp.json())
    except Exception:
        return {"_text": resp.text[:500]}


async def main() -> None:
    settings = get_settings()
    print(f"mode={settings.kiwoom_mode.value}  base={settings.rest_base_url}")
    if not settings.is_mock:
        raise SystemExit("ABORT: KIWOOM_MODE=mock 이 아닙니다. 실거래 모드에서 진단 스크립트 금지.")

    today = datetime.date.today().strftime("%Y%m%d")

    # 후보별 best-guess 바디 — 거절되면 return_msg 가 필수 필드를 알려준다.
    # stex_tp 는 ka10030 실측과 동일하게 3(통합).
    candidates: list[tuple[str, str, dict[str, Any]]] = [
        (
            "ka90009 외국인기관매매상위",
            "ka90009",
            {
                "mrkt_tp": MARKET_TP_ALL,  # 000=전체
                "amt_qty_tp": "1",  # 1=금액, 2=수량
                "qry_dt_tp": "1",  # 1=조회일자 포함
                "date": today,
                "stex_tp": "3",
            },
        ),
        (
            "ka10065 장중투자자별매매상위 (외국인)",
            "ka10065",
            {
                "trde_tp": "1",  # 1=순매수, 2=순매도
                "mrkt_tp": MARKET_TP_ALL,
                "orgn_tp": "9000",  # 9000=외국인, 9999=기관계
            },
        ),
        (
            "ka10035 외인연속순매매상위",
            "ka10035",
            {
                "mrkt_tp": MARKET_TP_ALL,
                "trde_tp": "2",  # 2=연속순매수
                "base_dt_tp": "0",  # 0=당일기준
                "stex_tp": "3",
            },
        ),
        (
            "ka10034 외인기간별매매상위",
            "ka10034",
            {
                "mrkt_tp": MARKET_TP_ALL,
                "trde_tp": "2",  # 2=순매수
                "dt": "0",  # 0=당일
                "stex_tp": "3",
            },
        ),
    ]

    async with KiwoomRestClient() as client:
        for label, tr_id, body in candidates:
            data = await _post_raw(client, tr_id, PATH_RANK, body)
            _dump(label, body, data)
            await asyncio.sleep(SLEEP_BETWEEN_CALLS_SEC)

        # 응답한 TR 이 있으면 시장 분리 조회(KOSPI 단독)도 되는지 확인 — 현행 발굴은
        # KOSPI/KOSDAQ 을 따로 부르고 병합하므로 mrkt_tp 분리 지원이 전제.
        data = await _post_raw(
            client,
            "ka90009",
            PATH_RANK,
            {
                "mrkt_tp": MARKET_TP_KOSPI,
                "amt_qty_tp": "1",
                "qry_dt_tp": "1",
                "date": today,
                "stex_tp": "3",
            },
        )
        _dump("ka90009 · KOSPI 단독 (시장 분리 지원 확인)", {"mrkt_tp": MARKET_TP_KOSPI}, data)

    print("\n--- 확인 포인트 ---")
    print("· return_code=0 인 후보가 있는가? (없으면 mock 미지원 → 폴백 축 재선정)")
    print("· 응답 본체의 list[dict] 첫 행 키: 종목코드(stk_cd)·종목명(stk_nm)·순매수 지표 필드명")
    print("· 순매수 금액/수량 필드가 음수 부호를 어떻게 표기하는지 (부호 유지 필요 — abs 금지)")
    print("· 1511 이면 return_msg 가 지목한 필드를 바디에 추가해 재실행")


if __name__ == "__main__":
    asyncio.run(main())
