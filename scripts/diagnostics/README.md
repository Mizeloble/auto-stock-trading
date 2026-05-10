# scripts/diagnostics — 키움 API raw 응답 캡처

운영용이 아니라 **개발자 진단 도구**. `KiwoomRestClient` 가 `return_code ≠ 0` 면 `KiwoomError` 로 raise 하므로 응답 본문 자체를 보고 싶을 때 쓴다.

## 도구

| 파일 | 용도 |
|---|---|
| `probe_orders.py` | 모의 주문 응답 dict 캡처 — `ord_no`/`odno` 등 주문번호 필드명 확인 (`account/orders.py` 매핑 검증) |
| `probe_rank_index.py` | 거래량 순위 + 업종 일봉 응답 dict 캡처 — `TR.RANK_VOLUME` / `TR.SECTOR_INDEX_DAILY` 필드명 확인 (`market/rank.py`, `market/index.py` 후보 체인 보정) |

## 실행

```sh
# 환경: KIWOOM_APP_KEY / KIWOOM_APP_SECRET 가 .env 또는 환경변수에 있어야 함
# probe_rank_index.py 는 KIWOOM_MODE=mock 만 동작 (실거래 차단)

uv run python scripts/diagnostics/probe_orders.py
uv run python scripts/diagnostics/probe_rank_index.py
```

응답에 포함된 시크릿(`secret`, `token`, `key` 키)은 자동 마스킹된다 (`_redact()`).

## 언제 다시 쓰나

- 키움이 응답 스키마를 바꿔 단위 테스트가 깨졌는데 디버그가 어려울 때
- 새 TR ID 를 추가하기 전에 실제 응답 형태를 확인할 때
- 주문 거절 사유 (예: A prefix · `_AL` suffix) 같은 응답 패턴을 실측으로 잡을 때

상세 사용 맥락은 `docs/history.md` (또는 `todo-list.md`) 의 21단계 (probe_orders), 22단계 (probe_rank_index) 항목 참고.
