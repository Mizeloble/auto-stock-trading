# news/ 규칙

- 주간 자동화 파이프라인. `collector.py`(RSS 수집) → `proposal.py`(LLM 으로 `DaytradeProposal` 생성, Phase B) → `profile_writer.py`(`daytrade-current.yaml` 자동 갱신, Phase C) → 토요일 cron 텔레그램 푸시(Phase D).
- `profile_writer` 는 LLM 호출을 **하지 않는다**(그건 Phase B). 결정론적: 같은 proposal + 같은 current_profile → 같은 결과.
- **자동화 폭주 방지가 핵심**: `SafetyCaps` 한도를 벗어나는 변경은 거부 + 경고, 적용 전 `daytrade-current.yaml.bak.YYYY-MM-DD` 백업, 변경 diff 를 한국어로 반환. 캡 완화는 사용자 확인 후에만(사용자가 직접 검토할 수 있어야 함).
- LLM 호출은 반드시 `llm/` 비용 가드 경유. 주 1회 이하 cron 외 호출 추가 금지.
- 수집/제안 실패가 트레이딩·리포트를 막지 않게 — 독립 cron, best-effort.
