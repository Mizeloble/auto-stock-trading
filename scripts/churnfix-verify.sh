#!/bin/zsh
# 1회성 — 2026-05-16 적용한 과매매 축소 프로파일 효과 검증.
#
# 무엇/왜:
# - 2026-05-16 daytrade-current.yaml 변경: cooldown_min 30→90,
#   daily_notional_limit 0→20,000,000 (커밋 928ddf2/ce552de 후속 운영 조치).
# - 원격 스케줄은 data/reports·logs 가 .gitignore 라 접근 불가 → 로컬 launchd 1회샷.
# - cron 은 수치 수집·푸시만. 캡 조정 판단은 사람/Claude 가 이 수치 보고 결정
#   (메모리 project_after_cost_loss: 깜깜이 단일파라미터 튜닝 금지, 회전율 근거).
# - 발화 후 스스로 launchd 에서 bootout + plist 제거 → 진짜 1회성 (내년 재발화 X).
#
# 어떻게 확인:
# - 실행 후 logs/churnfix-verify-2026-05-21.log 생성, 텔레그램에 "🔧 과매매 검증" 도착.

set -euo pipefail

PROJECT_DIR="/Users/jjmize/Project/auto-stock-trading"
UV_BIN="/opt/homebrew/bin/uv"
LABEL="com.mizeloble.auto-trade.churnfix-verify"

cd "$PROJECT_DIR"
mkdir -p logs

LOG="logs/churnfix-verify-2026-05-21.log"

# 5/18~20 일별 리포트 재생성 (로그 없으면 해당일 스킵 — 휴장/미실행 허용).
for d in 2026-05-18 2026-05-19 2026-05-20; do
  "$UV_BIN" run auto-trade report --date "$d" >/dev/null 2>&1 || true
done

# 수치 수집 + 한국어 요약 + 텔레그램 푸시 (runner.weekly_proposal = text 패스스루).
"$UV_BIN" run python - <<'PY' >> "$LOG" 2>&1
import asyncio, glob, json, re
from datetime import datetime

DAYS = ["2026-05-18", "2026-05-19", "2026-05-20"]
REF_CASH = 10_000_000  # paper-day mock 기준 예수금 (참고 분모)

lines = ["🔧 과매매 수정 검증 (5/18~20)", ""]
have_any = False
for d in DAYS:
    p = f"data/reports/daily-{d}.json"
    try:
        r = json.load(open(p, encoding="utf-8"))
    except FileNotFoundError:
        lines.append(f"• {d}: 리포트 없음 (미실행/휴장)")
        continue
    have_any = True
    buys = r.get("buys") or []
    notional = sum((b.get("price") or 0) * b.get("quantity", 0) for b in buys)
    turn = notional / REF_CASH if REF_CASH else 0
    rp = r.get("realized_pnl")
    ac = r.get("realized_pnl_after_cost")
    tax = r.get("total_taxes")
    # 'daily notional limit reached' 발생 횟수 (캡이 정상매매까지 굶기는지 신호).
    hits = 0
    for lg in glob.glob(f"logs/paper-day-{d}.log"):
        try:
            with open(lg, encoding="utf-8", errors="replace") as fh:
                hits += sum(1 for ln in fh if "daily notional limit reached" in ln)
        except OSError:
            pass
    rp_s = f"{rp:+,.0f}" if isinstance(rp, (int, float)) else "—"
    ac_s = f"{ac:+,.0f}" if isinstance(ac, (int, float)) else "—"
    tax_s = f"{tax:,.0f}" if isinstance(tax, (int, float)) else "—"
    lines.append(
        f"• {d}: 매수회전 {notional:,.0f}원(예수금 {turn:.1f}x) · "
        f"실현 {rp_s} / 비용차감 {ac_s} · 매도세 {tax_s} · "
        f"한도도달 {hits}건"
    )

lines.append("")
if have_any:
    lines.append(
        "이전 기준: 5/12 매수회전 ~78.8M(8x)·비용차감 -458,545원. "
        "회전이 줄고 비용차감 손익이 개선됐는지, '한도도달'이 과해 정상매매까지 "
        "막는지 보고 캡(20M) 조정 여부를 Claude 에게 검토 요청해 주세요. "
        "롤백: strategies/profiles/daytrade-current.yaml.bak.churnfix-2026-05-16."
    )
else:
    lines.append(
        "5/18~20 거래 데이터가 없습니다(휴장/미실행). 거래일 누적 후 다시 점검 필요."
    )

text = "\n".join(lines)
print(f"[{datetime.now().isoformat(timespec='seconds')}] churnfix-verify\n{text}")

async def _push() -> None:
    try:
        from auto_trading.notify.factory import build_notifier
        n = build_notifier()
        await n.notify("runner.weekly_proposal", text=text)
        close = getattr(n, "aclose", None)
        if close:
            await close()
    except Exception as e:  # 알림 실패가 검증 자체를 막지 않도록 swallow
        print(f"telegram push 실패(무시): {e}")

asyncio.run(_push())
PY

# 1회성 — 스스로 launchd 에서 제거 (내년 같은 날 재발화 방지).
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist" 2>/dev/null || true

exit 0
