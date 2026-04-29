#!/bin/zsh
# launchd 가 평일 장 시작 직전(08:55 KST) 트리거하는 스크립트.
#
# 무엇/왜:
# - caffeinate -i : 스크립트가 도는 동안 시스템 sleep 차단(디스플레이는 자도 됨)
# - exec : 자식 프로세스가 직접 launchd 의 자식이 되어 launchd 가 정확히 추적·종료
# - 표준출력/에러는 logs/paper-YYYY-MM-DD.log 에 누적
#
# 어떻게 확인:
# - 실행 직후 logs/ 에 오늘 날짜 파일 생성, 텔레그램 "▶️ paper 시작" 푸시 도착
# - 장 마감 후 paper-stop.sh 가 SIGTERM → 러너 graceful shutdown

set -euo pipefail

PROJECT_DIR="/Users/jjmize/Project/auto-stock-trading"
UV_BIN="/opt/homebrew/bin/uv"
CAFFEINATE="/usr/bin/caffeinate"

cd "$PROJECT_DIR"
mkdir -p logs

LOG="logs/paper-$(date +%Y-%m-%d).log"

# 동작은 strategies/profiles/auto_005930.yaml 의 내용으로 결정 — 전략·파라미터 변경은
# YAML 만 수정하면 다음 시동 때 자동 반영. --dry-run 은 모의 주문(시뮬레이션)만 로깅.
exec "$CAFFEINATE" -i "$UV_BIN" run auto-trade paper \
  --profile strategies/profiles/auto_005930.yaml \
  --dry-run \
  >> "$LOG" 2>&1
