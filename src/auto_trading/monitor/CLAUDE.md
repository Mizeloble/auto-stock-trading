# monitor/ 규칙

- 운영 진단 **read-only** 모듈. 트레이딩 핫패스에 영향을 주면 안 된다 — 모든 기록은 best-effort, 디스크 쓰기 실패는 swallow + 로그.
- `latency.py`: 신호→키움 응답 소요 시간을 JSONL append(`Settings.latency_log_path`, 운영 `data/latency.jsonl`) + 일별 통계(count/mean/p50/p95/p99). 실거래–백테스트 PnL 괴리 진단용. 패턴은 `llm/cost_tracker.py` 와 동일(append-only, 손상 라인 스킵).
- `tick_recorder.py`: 유니버스 0B 틱 원본을 일별 JSONL(`data/ticks/ticks-YYYY-MM-DD.jsonl`)로 기록. 핫패스 보호를 위해 메모리 버퍼 → 임계(200줄)/간격(5초) 도달 시 묶음 append. 트레이딩 판단에 절대 관여하지 않는다 — 틱 기반 가드 반사실 분석용 수집 전용.
- `data_integrity.py`: daily 리포트·백테스트 결과·주문 저널 정합성 검사. 검사 함수는 **순수 read-only**(디스크 수정 X), 한 파일 손상이 다른 검사를 막지 않게 격리, 결과는 `IntegrityIssue` 리스트(빈 리스트면 OK).
- `launchd.py`: macOS LaunchAgent 상태 수집(plist 스캔 + 로컬 `launchctl print` exit code + 로그 mtime). 마지막 실행은 레거시 `launchd-*.log` 와 래퍼 스크립트의 날짜별 `<short>-YYYY-MM-DD.log`(insight-weekly 만 `insight-` 접두사, `_DATED_LOG_PREFIX`) 중 최신 mtime. plist 스케줄(StartCalendarInterval/StartInterval)을 파싱해 다음 예정 시각·7일 예상/실제 실행일도 산출 — launchd Weekday 는 0·7=일요일. 실패는 전부 None 으로 흡수(다른 OS·테스트 환경 호환). 표 출력은 `cli` 의 `ops` 명령 담당.
- 외부 호출 없음. 입력은 로컬 파일/스냅샷만, 출력은 결정론적. 임계 초과 시 알림/차단 결정은 이 모듈이 아니라 preflight/cron 책임.
