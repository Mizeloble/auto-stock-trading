#!/bin/zsh
# 매주 토요일 09:00 KST 트리거 — 직전 7일치 일일 리포트로 LLM 누적 분석.
#
# 무엇/왜:
# - 22단계 Phase B. 매일 LLM 호출은 비효율(동일 패턴 반복) — 주 1회로 비용/시그널 균형.
# - 메모리 `feedback_llm_cost.md`: 사람 명시 트리거 또는 사전 합의된 주 1회 이하 cron 만 허용.
# - 텔레그램에 한국어 인사이트 푸시 (--push) → 폰에서 다음 주 운영 결정.
# - 27단계 (주말 뉴스 보강): 토요일 06:00 `news-prefetch` cron 이 RSS 캐시 선반영 →
#   여기 09:00 `weekly-proposal` 은 `force_refresh` default 로 새 뉴스를 누적 병합.
#
# 어떻게 확인:
# - 실행 직후 logs/insight-YYYY-MM-DD.log 생성, 텔레그램 "🧭 주간 인사이트" 푸시 도착.
# - data/reports/daily-*.json 이 1개도 없으면 CLI 가 Exit(1) — 며칠 운영 후 의미 있음.

set -euo pipefail

PROJECT_DIR="/Users/jjmize/Project/auto-stock-trading"
UV_BIN="/opt/homebrew/bin/uv"

cd "$PROJECT_DIR"
mkdir -p logs

LOG="logs/insight-$(date +%Y-%m-%d).log"

# 22단계 Phase B — 주간 인사이트 (사람용 결과 요약).
"$UV_BIN" run auto-trade insight --days 7 --push >> "$LOG" 2>&1

# 24단계 Phase D — 다음 주 day-trade 프로파일 자동 갱신.
# 인사이트 푸시는 이미 됐으므로, 제안 단계 실패해도 cron 자체는 성공으로 마감(|| true).
# data/news 수집 + LLM 1회 호출 + strategies/profiles/daytrade-current.yaml 갱신 + 백업 + 텔레그램 푸시.
"$UV_BIN" run auto-trade weekly-proposal --push >> "$LOG" 2>&1 || true

exit 0
