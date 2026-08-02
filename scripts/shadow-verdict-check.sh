#!/bin/zsh
# 섀도우 SMA 게이트 완화 판정 워처 — 표본 15일이 차는 날 텔레그램으로 1회 통보.
#
# 무엇/왜:
# - shadow_gate_study.py 는 표본 15일 미만이면 "판정 보류"만 출력하고, 그 전까지
#   아무도 돌리지 않으면 판정이 나와도 알 수 없다 (2026-07-31 판정 2건은 세션이
#   수동 실행 — 자동 경로 부재가 2026-08-02 재검토에서 확인됨).
# - 이 워처는 평일 장 마감 후(15:50) 판정 스크립트를 돌려 "판정 보류"면 로그만 남기고,
#   판정이 나오면 종합 수치를 텔레그램으로 1회 푸시한 뒤 스스로 launchd 에서 제거된다
#   (churnfix-verify.sh 와 같은 발화-후-자가-제거 패턴).
# - cron 은 수치 수집·푸시만 한다. 게이트 완화 여부 결정은 사람/Claude 몫
#   (LLM 호출 없음 — 비용 규율 준수).
# - 중복 방지: data/studies/verdict-notify-state.json 의 shadow_gate 키 기록
#   (alt_rank·snapshot_selection 판정과 같은 상태 파일).
#
# 어떻게 확인:
# - 수동 실행 시 표본 미달이면 logs/shadow-verdict-check.log 에 "보류" 한 줄만 추가.
# - 판정일에는 텔레그램 "👻 섀도우 게이트 판정" 도착 + launchd 목록에서 사라짐.

set -euo pipefail

PROJECT_DIR="/Users/jjmize/Project/auto-stock-trading"
UV_BIN="/opt/homebrew/bin/uv"
LABEL="com.mizeloble.auto-trade.shadow-verdict"
STATE="data/studies/verdict-notify-state.json"
LOG="logs/shadow-verdict-check.log"

cd "$PROJECT_DIR"
mkdir -p logs data/studies

# 주말 가드 (plist 는 StartCalendarInterval 단순화 — 요일 판단은 여기서).
# 수동 점검 시 SHADOW_VERDICT_FORCE=1 로 우회 가능.
weekday=$(date +%u)
if [ "${SHADOW_VERDICT_FORCE:-0}" != "1" ] && [ "$weekday" -gt 5 ]; then
  exit 0
fi

# 이미 통보했으면 재발화 금지 + 자가 제거 (재설치 대비 이중 안전).
if [ -f "$STATE" ] && grep -q '"shadow_gate"' "$STATE"; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/$LABEL.plist" 2>/dev/null || true
  exit 0
fi

OUTPUT=$("$UV_BIN" run python scripts/diagnostics/shadow_gate_study.py 2>/dev/null || true)

if [ -z "$OUTPUT" ]; then
  echo "[$(date '+%F %T')] 스크립트 출력 없음 — 데이터/환경 확인 필요" >> "$LOG"
  exit 0
fi

if printf '%s' "$OUTPUT" | grep -q "판정 보류"; then
  days=$(printf '%s' "$OUTPUT" | grep -o "현재 [0-9]*일" | head -1 || true)
  echo "[$(date '+%F %T')] 판정 보류 (${days:-표본 미상}) — 대기 계속" >> "$LOG"
  exit 0
fi

# ── 판정 도달: 종합 구간만 잘라 텔레그램 푸시 ──────────────────────────────
SUMMARY=$(printf '%s' "$OUTPUT" | sed -n '/--- 종합 ---/,$p')
export SHADOW_VERDICT_SUMMARY="$SUMMARY"

"$UV_BIN" run python - <<'PY' >> "$LOG" 2>&1
import asyncio
import json
import os
from datetime import date
from pathlib import Path

summary = os.environ.get("SHADOW_VERDICT_SUMMARY", "").strip()

text = "\n".join(
    [
        "👻 섀도우 게이트 판정 — 표본 15일 도달",
        "",
        "지수 이동평균 게이트(코스피·코스닥이 평균선 아래면 매수 전면 차단)를",
        "\"시장 체온계(breadth) 통과일에는 면제\"해도 되는지에 대한 가상매매 실측이",
        "판정 가능한 표본에 도달했습니다.",
        "",
        summary,
        "",
        "읽는 법: 'breadth 통과일' 줄이 면제 가설의 성적입니다.",
        "합계가 플러스면 완화 검토 근거 👍, 마이너스면 현행 유지 근거 👎.",
        "다음 단계: 이 수치는 자동 판단되지 않습니다 — Claude 에게",
        "\"섀도우 게이트 판정 검토해줘\"라고 요청해 주세요.",
    ]
)

print(f"[{date.today()}] 판정 도달 — 텔레그램 푸시 시도")


async def _push() -> None:
    try:
        from auto_trading.notify.factory import build_notifier

        n = build_notifier()
        await n.notify("runner.weekly_proposal", text=text)
        close = getattr(n, "aclose", None)
        if close:
            await close()
        print("텔레그램 푸시 완료")
    except Exception as e:  # 알림 실패가 상태 기록을 막지 않도록
        print(f"telegram push 실패(무시): {e}")


asyncio.run(_push())

# 상태 기록 — 같은 판정 재통보 방지 (alt_rank 등과 같은 파일).
state_path = Path("data/studies/verdict-notify-state.json")
state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
state["shadow_gate"] = {
    "notified_on": str(date.today()),
    "verdict": "표본 도달 — 수치 통보 완료, 완화 여부 결정은 사람/Claude 검토 대기",
}
state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("verdict-notify-state.json 기록 완료")
PY

# 발화 완료 — 스스로 launchd 에서 제거 (1회성 종료).
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist" 2>/dev/null || true

exit 0
