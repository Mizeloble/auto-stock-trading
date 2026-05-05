#!/bin/zsh
# 매주 토요일 09:00 KST 트리거 — 직전 7일치 일일 리포트로 LLM 누적 분석.
#
# 무엇/왜:
# - 22단계 Phase B. 매일 LLM 호출은 비효율(동일 패턴 반복) — 주 1회로 비용/시그널 균형.
# - 메모리 `feedback_llm_cost.md`: 사람 명시 트리거 또는 사전 합의된 주 1회 이하 cron 만 허용.
# - 텔레그램에 한국어 인사이트 푸시 (--push) → 폰에서 다음 주 운영 결정.
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

# 운영 사양 변경(--days, --reports-root) 은 이 줄만 수정. 키 없으면 CLI 가 Exit(1).
exec "$UV_BIN" run auto-trade insight --days 7 --push >> "$LOG" 2>&1
