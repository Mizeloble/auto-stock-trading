# runner/ 규칙

- `PaperRunner`(`paper.py`)가 모의/실거래 라이브 루프. `__init__.py` 는 재수출만 — 구현을 다시 넣지 말 것. **실거래 가드는 이 모듈 책임**: `settings.is_mock` 가 False 일 때는 `allow_real=True` 가 함께 주입된 경우에만 주문을 전송한다(account/ 는 무분기 호출만 — 가드를 여기서 막는다).
- 신호 흐름: tick → `aggregator._BarAggregator` 가 분봉 확정 → strategy.on_bar → risk 평가 → 주문. aggregator 는 PaperRunner 상태에 의존하지 않게(독립 테스트용) 유지 — runner 상태를 끌어다 쓰지 말 것.
- 외부 제어 상태는 `control.RunnerControlState`(RUNNING/PAUSED/STOPPING). 텔레그램 `/pause`·`/resume`·`/stop` 가 바꾼다. **pause 는 신호 평가는 계속하고 주문만 보류** — 정지가 아니다.
- 백그라운드 태스크(청산 루프·heartbeat·벽시계 공백 감시 `wallclock.py`)는 틱이 멈춰도 graceful stop 이 가능해야 하므로 `ws.signal_stop()` 경로를 보존한다. 정지 신호가 메인 루프에만 의존하게 만들지 말 것.
- `dry_run` 은 REST 미전송 시뮬레이션. 라이브 경로와 분기를 최소화해 둘이 갈라지지 않게 한다(왜: dry_run 으로 검증한 동작이 라이브와 같아야 신뢰 가능).
- **섀도우 세션**(`enable_shadow`)은 dry_run 강제 + 잔고 조회를 `shadow/` 가상 원장으로 대체 — 게이트 차단일 데이터 수집 전용. 시뮬 체결이 원장에 쌓여 guard·청산이 실제처럼 돌지만, 실주문·day-state 디스크·`daily-*.json` 에는 절대 손대지 않는다(리포트는 `shadow-*.json`).
- 종목 코드는 사용자에게 그대로 보이지 않게 — 이름 매핑 스냅샷(`notify/state.py`)을 거쳐 텔레그램으로 나간다.
