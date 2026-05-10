#!/bin/zsh
# 토요일 06:00 KST 트리거 — RSS 뉴스 미리 fetch (LLM 호출 없음 · 비용 0).
#
# 무엇/왜:
# - 27단계 (주말 뉴스 컨텍스트 보강). 09:00 weekly-proposal 이 사용할 캐시를
#   3시간 일찍 채워서, 미국 시장 마감(KST 토요일 06:00 직후) 후 한국어 보도가
#   RSS 에 게시되는 시간대를 더 길게 커버.
# - 09:00 호출은 `--force-refresh` 가 default 이므로 06:00 캐시 + 신규 뉴스를
#   url 키로 누적 병합 → prefetch 가 실패해도 안전.
#
# 어떻게 확인:
# - logs/news-prefetch-YYYY-MM-DD.log 가 매주 토요일 생성, 콘솔 건수 표시.
# - data/news/news-v3-YYYY-MM-DD.json 파일이 06:00 무렵 갱신.

set -euo pipefail

PROJECT_DIR="/Users/jjmize/Project/auto-stock-trading"
UV_BIN="/opt/homebrew/bin/uv"

cd "$PROJECT_DIR"
mkdir -p logs

LOG="logs/news-prefetch-$(date +%Y-%m-%d).log"

"$UV_BIN" run auto-trade fetch-news --days 7 >> "$LOG" 2>&1

exit 0
