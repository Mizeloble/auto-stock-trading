#!/bin/zsh
# launchd 가 평일 장 마감 직후(15:35 KST) 트리거하는 종료 스크립트.
#
# 무엇/왜:
# - paper-day.sh 가 띄운 `auto-trade paper` 프로세스에 SIGTERM 전송 → 러너가
#   finally 블록에서 ws.stop / notifier "runner.stopped" 푸시까지 마무리.
# - SIGKILL 안 씀(데이터 흘림 방지). pgrep/pkill 패턴은 caffeinate·uv 가 아닌
#   실제 paper 프로세스만 잡도록 명시적으로 좁힘.
#
# 어떻게 확인:
# - 종료 후 logs/paper-*.log 끝줄에 "runner.stopped" 가 있고
#   텔레그램에 "⏹ paper 종료" 푸시 도착.

set -euo pipefail

LOG_DIR="$(cd "$(dirname "$0")/../.." && pwd)/logs"
LOG="$LOG_DIR/paper-stop-$(date +%Y-%m-%d).log"
mkdir -p "$LOG_DIR"

{
  echo "=== $(date -Iseconds) paper-stop.sh 실행 ==="
  if pgrep -f "auto-trade paper" >/dev/null; then
    pkill -TERM -f "auto-trade paper"
    echo "SIGTERM 전송 완료"
  else
    echo "실행 중인 auto-trade paper 프로세스 없음"
  fi
} >> "$LOG" 2>&1
