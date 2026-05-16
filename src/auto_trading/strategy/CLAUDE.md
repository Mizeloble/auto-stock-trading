# strategy/ 규칙

- 전략은 `on_bar` / `on_tick` 에서 **`list[Signal]` 만 반환**. 주문 API 호출 금지.
- 네트워크/파일/시계(`datetime.now`) 등 부작용 금지 — 입력 bar 의 timestamp 만 사용. 결정론성이 백테스트 신뢰의 핵심.
- 새 전략은 `@register("이름")` 데코레이터로 등록한다. CLI 에서 이 "이름" 으로 호출 가능해야 한다.
- 내부 상태는 `self.state: dict` 에만 저장(직렬화·리셋 일관성).
- `Signal.price_hint`(`Decimal | None`): 전략이 제시하는 주문가 힌트. 리스크 매니저(`risk/manager.py`)가 None 이면 시장가, 있으면 이 값을 주문가로 쓴다. 수량은 여전히 리스크 매니저가 결정 — 전략이 정하지 않는다.
- swing 모드 전략 설정은 `profile.StrategyProfile` YAML(`strategies/profiles/*.yaml`). load/dump 는 이 한 모듈, 사용자가 직접 편집하는 스키마이므로 필드 변경 시 테스트(`test_profile_yaml`)를 함께 갱신한다(왜: 사용자가 코드 대신 YAML 로 전략을 짜는 흐름을 선호).
