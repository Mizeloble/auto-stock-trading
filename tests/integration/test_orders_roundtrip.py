"""실 모의서버 주문 거절 플로우 통합 테스트.

성공 응답(체결 접수)은 평일 장중에만 받을 수 있어 매핑 검증이 부분적이다.
이 파일은 평일/주말 어느 때나 일관되게 거절 응답을 받는 호출 (가짜 원주문번호로
취소 요청) 을 사용해, 키움이 비즈니스 에러를 `KiwoomError` 로 surface 하는지만 검증.

성공 응답의 ord_no/odno 매핑은 평일 장중에 `scripts/probe_orders.py` 로 재검증한 후
별도 테스트를 추가한다.
"""

from __future__ import annotations

import pytest

from auto_trading.account.orders import cancel_order
from auto_trading.kiwoom.rest import KiwoomRestClient
from auto_trading.kiwoom.schemas import KiwoomError

pytestmark = pytest.mark.integration


async def test_cancel_with_unknown_order_no_raises_kiwoom_error(
    kiwoom_client: KiwoomRestClient,
) -> None:
    """존재하지 않는 원주문번호로 취소 요청 → 키움이 거절 → KiwoomError 로 매핑."""
    with pytest.raises(KiwoomError) as exc:
        await cancel_order(
            kiwoom_client,
            original_order_no="0000001",
            symbol="005930",
            quantity=1,
        )

    # return_code 가 0 이 아닌 어떤 비즈니스 에러여도 OK — 메시지 본문 단정은 회피
    assert exc.value.code != "0"
    assert exc.value.message
