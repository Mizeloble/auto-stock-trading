#!/bin/zsh
# 로그 자동 정리 — 매일 02:30 KST launchd 트리거 (paper-day/insight 와 충돌 X).
#
# 무엇/왜:
# - 매일 paper-day/paper/insight/weekly-proposal 로그가 100~250KB 씩 누적.
#   별다른 정리 없이 1년이면 36~90MB + 검색 속도 저하.
# - launchd 자체 stdout/stderr (`launchd-*.log`) 는 append-only 라 회전 대신
#   10MB 초과 시 truncate 만.
#
# 정책:
# - 30일↑ `*.log` (단, launchd-*.log 제외) → gzip -9 (`*.log.gz`)
# - 90일↑ `*.log.gz` → 삭제
# - launchd-*.log 가 10MB 넘으면 :> 로 비우고 truncate 시각 1줄
#
# 어떻게 확인:
# - 수동: `LOGS_ROOT=/tmp/test-logs zsh scripts/rotate-logs.sh`
# - launchd 등록 후: `launchctl list | grep mizeloble.auto-trade.log-rotate`
# - 다음 02:30 이후: `ls -la logs/` 에서 `.log.gz` 출현 / launchd-*.log 시작에 truncated_at= 라인

set -euo pipefail

LOGS_ROOT="${LOGS_ROOT:-/Users/jjmize/Project/auto-stock-trading/logs}"

if [[ ! -d "$LOGS_ROOT" ]]; then
  echo "logs dir not found: $LOGS_ROOT" >&2
  exit 0
fi

# 30일↑ 일반 로그 → gzip. launchd-*.log 는 launchd 가 항상 append 중이라 제외.
find "$LOGS_ROOT" -maxdepth 1 -type f -name "*.log" -mtime +30 \
  ! -name "launchd-*.log" \
  -exec gzip -9 {} \;

# 90일↑ 압축 로그 → 삭제
find "$LOGS_ROOT" -maxdepth 1 -type f -name "*.log.gz" -mtime +90 -delete

# launchd 자체 로그가 10MB 넘으면 truncate (로테이트 X — launchd 가 같은 fd 유지)
LAUNCHD_LIMIT=$((10 * 1024 * 1024))
for f in "$LOGS_ROOT"/launchd-*.log(N); do
  size=$(stat -f%z "$f" 2>/dev/null || echo 0)
  if (( size > LAUNCHD_LIMIT )); then
    : > "$f"
    echo "truncated_at=$(date -u +%Y-%m-%dT%H:%M:%SZ) prev_size=${size}" >> "$f"
  fi
done
