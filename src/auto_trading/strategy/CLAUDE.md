# strategy/ 규칙

- 전략은 `on_bar` / `on_tick` 에서 **`list[Signal]` 만 반환**. 주문 API 호출 금지.
- 네트워크/파일/시계(`datetime.now`) 등 부작용 금지 — 입력 bar 의 timestamp 만 사용. 결정론성이 백테스트 신뢰의 핵심.
- 새 전략은 `@register("이름")` 데코레이터로 등록한다. CLI 에서 이 "이름" 으로 호출 가능해야 한다.
- 내부 상태는 `self.state: dict` 에만 저장(직렬화·리셋 일관성).
