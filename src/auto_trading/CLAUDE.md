# src/auto_trading 규칙

- 설정값은 반드시 `config.get_settings()` 를 통해 읽는다. `os.environ` 직접 호출 금지.
- 외부 네트워크 호출은 `kiwoom/` 모듈을 경유 — 다른 모듈에서 `httpx` 직접 쓰지 말 것.
- 로깅은 `structlog.get_logger(__name__)` 사용. `print()` 금지.
- 도메인 경계: `market/` 은 시세만, `account/` 는 잔고·주문만, `strategy/` 는 판단만, `risk/` 는 필터만. 서로 책임을 넘지 않는다.
