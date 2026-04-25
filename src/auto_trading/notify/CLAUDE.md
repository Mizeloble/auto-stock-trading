# notify/ 규칙

- **알림 실패가 트레이딩을 막아선 안 된다.** `notify()` 는 어떤 예외도 raise 하지 말고 `log.warning("notify.send_failed", ...)` 로 swallow.
- 봇 토큰·chat_id 같은 시크릿은 메시지 본문/`__repr__`/`__str__`/예외 메시지에 절대 노출 금지. 키움 토큰 마스킹과 동일 (앞 4자 + `…`).
- 외부 호출은 모듈 자체의 `httpx.AsyncClient` 사용. `KiwoomRestClient` 재사용 금지 (도메인 다름, Bearer 헤더가 Telegram에 적용되면 곤란).
- 메시지 포맷팅(이벤트→사람이 읽는 한국어)은 notifier 내부 책임. 러너는 structlog 이벤트명 + kwargs 만 넘긴다.
