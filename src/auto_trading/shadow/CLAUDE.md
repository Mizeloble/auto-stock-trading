# shadow/ 규칙

- 라이브 데이터에서 **주문 송신 없이** 신규 전략의 가상 PnL 을 누적하는 read-only 모듈. paper-day 와 실거래 사이의 마지막 검증층(백테스트–라이브 괴리 정량 진단).
- `tracker.ShadowTracker` 는 외부 호출 0 — 가격·시그널을 호출자가 주입하고 모듈은 누적·기록만 한다(결정론 보존). **실주문/잔고 변경 호출 절대 금지**.
- 가정: 모든 BUY 즉시 체결 + SELL 은 FIFO 매칭. 수수료/거래세/슬리피지 기본값은 `SimulatedBroker` 와 동일하게 유지 — 백테스트와 같은 가정이어야 괴리 비교가 의미 있다(값을 바꿀 때 backtest/broker.py·reports/daily.py 와 함께).
- `log_path` JSONL 영속(append-only) → 봇 재시작 후 `replay()` 로 누적 복구. 기록 실패가 라이브 트레이딩을 막지 않게 swallow.
