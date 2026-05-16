# llm/ 규칙

- Claude API 호출의 **비용 추적·월 한도 가드 전용**. `cost_tracker.py` 는 순수 모듈(JSONL append + 월 합산 + 한도 검사), `cost.py` 는 호출 지점과 잇는 어댑터(`check_budget_or_raise` → `LLMBudgetExceeded`, `record_response`).
- 사용 패턴 고정: 호출 **직전** `check_budget_or_raise()`, 호출 **직후** `record_response()`. 알림(텔레그램)은 이 모듈에서 하지 않는다 — preflight/cron 이 `budget_status` 를 보고 결정(왜: 호출 핫패스에 알림 의존성을 묶지 않기 위함).
- 모델별 단가는 `cost_tracker.MODEL_PRICES_USD_PER_M` **한 곳**에서만 수정. 모르는 모델은 가장 비싼 가격으로 **과대 추정** — 한도를 보호하는 안전한 방향이므로 바꾸지 말 것.
- 호출 빈도: 사람 명령 또는 사전 합의된 **주 1회 이하 cron** 만. 매일/매시간 호출 코드를 추가하지 않는다(프로젝트 비용 규율).
- JSONL 쓰기 실패가 LLM 호출 자체를 막지 않게 swallow + 로그. `anthropic_api_key` 는 로그·예외에 노출 금지.
