#!/bin/zsh
# launchd 가 평일 장 시작 직전(08:55 KST) 트리거하는 day-trade 모드 스크립트.
#
# 무엇/왜:
# - 21단계 day-trade 모드. 매일 거래량 상위 3종목 발굴 → 즉시 매수 → 15:15 청산.
# - 기존 paper-day.sh(swing 모드) 와 별도. 둘 중 하나만 활성화 — manual.md 참고.
# - caffeinate -i : 스크립트가 도는 동안 시스템 sleep 차단(디스플레이는 자도 됨)
# - exec : 자식 프로세스가 직접 launchd 의 자식이 되어 launchd 가 정확히 추적·종료
# - 표준출력/에러는 logs/paper-day-YYYY-MM-DD.log 에 누적
#
# 어떻게 확인:
# - 09:00 직전에 실행 직후 logs/ 에 오늘 날짜 파일 생성, 텔레그램 시작 푸시 도착
# - 09:00:30 발굴 후 universe_chosen / order_simulated 로그 누적
# - 15:15 도달 시 liquidate_simulated → runner.stopped 후 자기 종료 (별도 stop plist 불필요)

set -euo pipefail

PROJECT_DIR="/Users/jjmize/Project/auto-stock-trading"
UV_BIN="/opt/homebrew/bin/uv"
CAFFEINATE="/usr/bin/caffeinate"

cd "$PROJECT_DIR"
mkdir -p logs

LOG="logs/paper-day-$(date +%Y-%m-%d).log"

# 운영 사양 변경(top_n, 청산 시각 등) 은 이 줄만 수정. --dry-run 은 모의 시뮬.
# 실주문 전환은 며칠 dry-run 검증 후 사용자 명시 승인 필요 — 그때만 --no-dry-run 으로.
exec "$CAFFEINATE" -i "$UV_BIN" run auto-trade paper-day \
  --top-n 3 \
  --liquidate 15:15 \
  --dry-run \
  >> "$LOG" 2>&1
