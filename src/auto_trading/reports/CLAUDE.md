# reports/ 규칙

- 리포트는 **사후 집계만** — 러너의 트레이딩 핫패스에 영향 X. 모든 함수는 입력(snapshot/log path) 만 보고 결정론적으로 결과를 만든다.
- 로그 파서는 JSON 라인만 파싱. 비-JSON 라인(rich 콘솔 출력 등) 은 조용히 skip — 운영 중 stderr 가 섞여도 깨지지 않게.
- 외부 호출 금지. 키움 REST/WS/텔레그램 호출은 호출자(러너·CLI)가 책임.
- 직렬화는 JSON. `data/reports/daily-YYYY-MM-DD.json` 으로 저장 → Phase B(주간 인사이트) 가 직전 N일치 입력으로 사용.
- 로그 파싱은 `log_parser.py` 의 `LogEvents` **한 곳**. 신규 이벤트 필드를 더하면 여기와 `daily.py` 집계를 함께 갱신한다(중복 파서 만들지 말 것).
- **비용 상수 동기화(중요)**: `daily.py` 의 `COST_COMMISSION_RATE`/`COST_TAX_RATE_SELL`/`COST_SLIPPAGE_BPS` 는 `backtest/broker.py` 기본값과 수동으로 일치시킨다. import 로 안 묶는 이유는 reports/ 외부 의존 최소화 — 한쪽만 바꾸면 리포트 손익이 백테스트와 어긋난다.
