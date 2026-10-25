#!/bin/zsh
# 매일 08:50 KST launchd 가 트리거 — KRX 시총 상위 200 마스터 산출.
#
# 무엇/왜:
# - `snapshot-top200` 잡이 매 10분 호출할 *종목 풀*. 일 1회 갱신.
# - pykrx 로 KRX 공식 시총 받아 `data/snapshots/universe-YYYY-MM-DD.json` 저장.
# - 결과 데이터는 직전 거래일 종가 기준 (08:50 시점에는 오늘 종가가 아직 없음).
#
# 어떻게 확인:
# - 08:51 직후 logs/marketcap-universe-오늘.log 에 "✓ 시총 마스터 저장" 줄 출력.
# - `cat data/snapshots/universe-오늘.json | python -m json.tool | head -20`.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"  # 스크립트 위치 기준 저장소 루트 (scripts/ 의 부모)
UV_BIN="/opt/homebrew/bin/uv"

cd "$PROJECT_DIR"
mkdir -p logs

LOG="logs/marketcap-universe-$(date +%Y-%m-%d).log"

# 주말은 그냥 종료 — 평일 KRX 데이터가 의미 있음. 임시휴장은 pykrx 가 빈 응답 → 봇이
# fallback_to_yesterday 로 처리하므로 그냥 돌려도 OK.
WEEKDAY=$(date +%u)  # 1=월, ..., 7=일
if [ "$WEEKDAY" -ge 6 ]; then
  echo "주말($(date +%a)) — 시총 마스터 갱신 생략" >> "$LOG"
  exit 0
fi

exec "$UV_BIN" run auto-trade build-marketcap-universe --top-n 200 \
  >> "$LOG" 2>&1
