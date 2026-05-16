# monitor/ 규칙

- 운영 진단 **read-only** 모듈. 트레이딩 핫패스에 영향을 주면 안 된다 — 모든 기록은 best-effort, 디스크 쓰기 실패는 swallow + 로그.
- `latency.py`: 신호→키움 응답 소요 시간을 JSONL append(`Settings.latency_log_path`, 운영 `data/latency.jsonl`) + 일별 통계(count/mean/p50/p95/p99). 실거래–백테스트 PnL 괴리 진단용. 패턴은 `llm/cost_tracker.py` 와 동일(append-only, 손상 라인 스킵).
- `data_integrity.py`: daily 리포트·백테스트 결과·주문 저널 정합성 검사. 검사 함수는 **순수 read-only**(디스크 수정 X), 한 파일 손상이 다른 검사를 막지 않게 격리, 결과는 `IntegrityIssue` 리스트(빈 리스트면 OK).
- 외부 호출 없음. 입력은 로컬 파일/스냅샷만, 출력은 결정론적. 임계 초과 시 알림/차단 결정은 이 모듈이 아니라 preflight/cron 책임.
