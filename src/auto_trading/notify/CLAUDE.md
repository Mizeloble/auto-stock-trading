# notify/ 규칙

- **알림 실패가 트레이딩을 막아선 안 된다.** `notify()` 는 어떤 예외도 raise 하지 말고 `log.warning("notify.send_failed", ...)` 로 swallow.
- 봇 토큰·chat_id 같은 시크릿은 메시지 본문/`__repr__`/`__str__`/예외 메시지에 절대 노출 금지. 키움 토큰 마스킹과 동일 (앞 4자 + `…`).
- 외부 호출은 모듈 자체의 `httpx.AsyncClient` 사용. `KiwoomRestClient` 재사용 금지 (도메인 다름, Bearer 헤더가 Telegram에 적용되면 곤란).
- 메시지 포맷팅(이벤트→사람이 읽는 한국어)은 `telegram.py`(이벤트 템플릿)와 `state.py`(`RunnerStateSnapshot` 상태 스냅샷 포맷터)에 분산돼 있다. 러너는 structlog 이벤트명 + kwargs 만 넘기고, 종목 코드→이름 표시는 `RunnerStateSnapshot.symbol_names` 를 경유한다(코드 숫자만 찍지 말 것 — 초보 사용자 가독성).
- `ThrottledNotifier`(`throttle.py`): day-trade 매매 빈도가 높을 때 텔레그램 채터링을 막는다. order_accepted/failed·liquidate·position_guard 류는 N분 윈도우로 묶어 1건 요약, 시작/종료/kill switch/리포트 등 중요 이벤트는 즉시 통과(`_PASSTHROUGH`). 새 알림 추가 시 throttle 통과 대상인지 분류할 것.
