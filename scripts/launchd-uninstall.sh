#!/bin/zsh
# launchd 에서 paper / paper-stop 잡 제거.
#
# 어떻게 확인:
# - launchctl list | grep mizeloble.auto-trade  → 출력 없으면 성공

set -euo pipefail

PLIST_DST="$HOME/Library/LaunchAgents"
# 21단계 시점의 swing 잡(paper / paper-stop) 도 함께 정리 — 이전에 등록했던 사용자
# 위해 폭넓게 해제. 이미 해제됐으면 조용히 skip.
LABELS=(
  "com.mizeloble.auto-trade.paper-day"
  "com.mizeloble.auto-trade.news-prefetch"
  "com.mizeloble.auto-trade.insight-weekly"
  "com.mizeloble.auto-trade.log-rotate"
  "com.mizeloble.auto-trade.heartbeat-check"
  "com.mizeloble.auto-trade.paper"
  "com.mizeloble.auto-trade.paper-stop"
)

for label in $LABELS; do
  if launchctl print "gui/$UID/$label" >/dev/null 2>&1; then
    launchctl bootout "gui/$UID/$label"
    echo "  - 해제: $label"
  else
    echo "  - 이미 해제됨: $label"
  fi
  if [[ -f "$PLIST_DST/$label.plist" ]]; then
    rm "$PLIST_DST/$label.plist"
    echo "  - 삭제: $PLIST_DST/$label.plist"
  fi
done

echo
echo "✅ 해제 완료."
