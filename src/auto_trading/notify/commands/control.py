"""러너 제어 명령 핸들러 (`/pause` `/resume` `/stop`).

각 핸들러는 등록된 콜백이 None 이면 "비활성" 안내 문자열 반환 — 봇 인스턴스가
주문 송신 권한 없이 시동된 경우(읽기 전용 봇) 안전하게 거부.
"""

from __future__ import annotations

from collections.abc import Callable

ControlAction = Callable[[], None]


def handle_pause(on_pause: ControlAction | None) -> str:
    if on_pause is None:
        return "이 봇 인스턴스에선 /pause 가 비활성입니다."
    on_pause()
    return "⏸ 일시정지 — 시그널은 계속 기록되지만 주문 송신은 보류됩니다.\n/resume 으로 복귀."


def handle_resume(on_resume: ControlAction | None) -> str:
    if on_resume is None:
        return "이 봇 인스턴스에선 /resume 이 비활성입니다."
    on_resume()
    return "▶ 재개 — 정상 매매 모드로 복귀."


def handle_stop(on_stop: ControlAction | None) -> str:
    if on_stop is None:
        return "이 봇 인스턴스에선 /stop 이 비활성입니다."
    on_stop()
    return "⏹ 종료 요청됨 — 다음 봉에서 graceful shutdown.\n되돌릴 수 없습니다."
