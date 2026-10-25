#!/bin/zsh
# launchd 가 평일 09:00–15:30 KST 매 10분 트리거 — 시총 200 종목 현재가 스냅샷 1라운드.
#
# 무엇/왜:
# - 1라운드 = 200종목 × ka10001 호출 ≈ 30초. JSONL append 로 `data/snapshots/
#   topcap-YYYY-MM-DD.jsonl` 에 누적.
# - 매 호출이 독립 — 죽어도 다음 10분에 부활. heartbeat 잡 같은 monitor 불필요.
#
# 어떻게 확인:
# - 09:00 직후 logs/snapshot-top200-오늘.log 끝줄에 "✓ 스냅샷 1라운드: 200/200건".
# - `wc -l data/snapshots/topcap-오늘.jsonl` 가 라운드당 ~200 증가.
# - `tail -1 data/snapshots/topcap-오늘.jsonl | python -m json.tool` 로 스키마 확인.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"  # 스크립트 위치 기준 저장소 루트 (scripts/ 의 부모)
UV_BIN="/opt/homebrew/bin/uv"

cd "$PROJECT_DIR"
mkdir -p logs

LOG="logs/snapshot-top200-$(date +%Y-%m-%d).log"

# 주말은 종료 — plist 가 Weekday 안 지정해 매일 호출되므로 여기서 가드.
# (Weekday 5개 × 시각 40개 = 200 dict 보다 plist 가 가벼움.)
WEEKDAY=$(date +%u)
if [ "$WEEKDAY" -ge 6 ]; then
  echo "주말($(date +%a)) — 스냅샷 생략" >> "$LOG"
  exit 0
fi

# 라운드 시작 시각 + 종료 시각만 append (200줄 log spam 방지 — JSONL 본체는 data/ 에).
{
  echo "--- $(date '+%Y-%m-%d %H:%M:%S') 라운드 시작 ---"
  "$UV_BIN" run auto-trade snapshot-top200
  echo "--- $(date '+%Y-%m-%d %H:%M:%S') 라운드 종료 ---"
} >> "$LOG" 2>&1
