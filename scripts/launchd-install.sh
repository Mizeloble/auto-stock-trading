#!/bin/zsh
# launchd 에 day-trade 자동 운영 잡 등록.
#
# 무엇/왜:
# - ~/Library/LaunchAgents/ 에 plist 복사 후 bootstrap 으로 등록.
# - 등록 후 자동 사이클:
#   * 평일 08:55 KST: paper-day 시동 → 발굴·매수·15:15 자체 청산·종료
#   * 토요일 06:00 KST: news-prefetch cron → RSS 뉴스 미리 fetch (LLM X)
#   * 토요일 09:00 KST: insight-weekly cron → 인사이트 + weekly-proposal LLM 호출
# - 21단계 시점의 swing(`paper`) + paper-stop 은 day-trade 모드와 동시 운영 X.
#   필요하면 LABELS 에 추가하되 paper-day 와 충돌 없도록 plist 시각·계좌 점검.
# - bootstrap 은 macOS Catalina 이후 권장 명령. 구식 `launchctl load` 와 다름.
#
# 어떻게 확인:
# - launchctl list | grep mizeloble.auto-trade
# - 라벨 두 개 (paper-day + insight-weekly) 보이면 성공

set -euo pipefail

PROJECT_DIR="/Users/jjmize/Project/auto-stock-trading"
PLIST_SRC="$PROJECT_DIR/scripts/launchd"
PLIST_DST="$HOME/Library/LaunchAgents"
LABELS=(
  "com.mizeloble.auto-trade.paper-day"
  "com.mizeloble.auto-trade.news-prefetch"
  "com.mizeloble.auto-trade.insight-weekly"
  "com.mizeloble.auto-trade.log-rotate"
  "com.mizeloble.auto-trade.heartbeat-check"
)

mkdir -p "$PLIST_DST"
mkdir -p "$PROJECT_DIR/logs"

# 스크립트 실행권한 보장
chmod +x \
  "$PROJECT_DIR/scripts/paper-day-mode.sh" \
  "$PROJECT_DIR/scripts/news-prefetch.sh" \
  "$PROJECT_DIR/scripts/insight-weekly.sh" \
  "$PROJECT_DIR/scripts/rotate-logs.sh" \
  "$PROJECT_DIR/scripts/heartbeat-check.sh"

for label in $LABELS; do
  src="$PLIST_SRC/$label.plist"
  dst="$PLIST_DST/$label.plist"

  if [[ ! -f "$src" ]]; then
    echo "ERROR: plist 원본 없음: $src" >&2
    exit 1
  fi

  # 이미 등록돼 있으면 한 번 정리 (재등록을 위해)
  if launchctl print "gui/$UID/$label" >/dev/null 2>&1; then
    echo "  - 기존 등록 해제: $label"
    launchctl bootout "gui/$UID/$label" 2>/dev/null || true
  fi

  cp "$src" "$dst"
  echo "  - 복사: $dst"

  launchctl bootstrap "gui/$UID" "$dst"
  echo "  - 등록: $label"
done

echo
echo "✅ 등록 완료. 자동 사이클:"
echo "  - 평일 08:55 KST: paper-day 시동 → 15:15 자체 청산"
echo "  - 토요일 06:00 KST: news-prefetch (RSS 캐시 선반영, LLM X)"
echo "  - 토요일 09:00 KST: insight-weekly (인사이트 + weekly-proposal, force_refresh 누적)"
echo "  - 매일 02:30 KST: log-rotate (30일↑ 압축, 90일↑ 삭제, launchd-*.log truncate)"
echo "  - 5분마다 (평일 09-16 KST 만): heartbeat-check (봇 멈춤 감지 → 텔레그램 알림)"
echo "  확인: launchctl list | grep mizeloble.auto-trade"
echo "  강제 즉시 시동(테스트용): launchctl kickstart -k gui/\$UID/com.mizeloble.auto-trade.paper-day"
echo "  뉴스 prefetch 즉시 시동(테스트용): launchctl kickstart -k gui/\$UID/com.mizeloble.auto-trade.news-prefetch"
echo "  토요일 cron 즉시 시동(테스트용): launchctl kickstart -k gui/\$UID/com.mizeloble.auto-trade.insight-weekly"
echo "  로그 회전 즉시 시동(테스트용): launchctl kickstart -k gui/\$UID/com.mizeloble.auto-trade.log-rotate"
echo "  heartbeat-check 즉시 시동(테스트용): launchctl kickstart -k gui/\$UID/com.mizeloble.auto-trade.heartbeat-check"
