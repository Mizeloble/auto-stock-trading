#!/bin/zsh
# 5분 간격 launchd 트리거 — 봇 heartbeat 누락 검사 (#13).
#
# 무엇/왜:
# - 봇이 조용히 멈췄을 때(WebSocket 끊김 + 재연결 실패, 데드락) 사용자가
#   저녁에야 발견하는 위험을 막는다. cron 이 5분마다 실행, 1시간 갱신
#   누락 시 텔레그램에 stale 알림 1회 (cooldown 30분).
# - 평일 09:00~16:00 KST 만 검사 — 그 외엔 봇이 멈춰있는 게 정상이라
#   false positive 도배 방지. 시간 게이트는 plist 가 아니라 *셸 래퍼* 에
#   둠 — plist 의 StartCalendarInterval 로 표현하면 168개 entry 필요.
#
# 어떻게 확인:
# - 평일 장중 봇 SIGSTOP → 1시간 후 텔레그램에 "🚨 heartbeat stale" 알림.
# - 정상 운영 중에는 침묵. logs/launchd-heartbeat-check.*.log 에 ok 라인.

set -euo pipefail

PROJECT_DIR="/Users/jjmize/Project/auto-stock-trading"
UV_BIN="/opt/homebrew/bin/uv"

# 평일 (DOW: 1=월 ~ 7=일) + 장중 (09:00~16:00 KST) 만 검사.
DOW=$(TZ=Asia/Seoul date +%u)
HOUR=$(TZ=Asia/Seoul date +%H)

if (( DOW > 5 )); then
  exit 0  # 주말 — 봇이 멈춰있는 게 정상.
fi

# HOUR 는 "09" 같은 0-padded 문자열이라 산술 비교 위해 10# prefix.
if (( 10#$HOUR < 9 || 10#$HOUR >= 16 )); then
  exit 0  # 장 외 — 봇이 멈춰있는 게 정상.
fi

cd "$PROJECT_DIR"

"$UV_BIN" run auto-trade heartbeat-check
