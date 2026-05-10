"""러너 제어 명령 핸들러 (`/pause` `/resume` `/stop` `/unpause`).

각 핸들러는 등록된 콜백이 None 이면 "비활성" 안내 문자열 반환 — 봇 인스턴스가
주문 송신 권한 없이 시동된 경우(읽기 전용 봇) 안전하게 거부.
"""

from __future__ import annotations

from collections.abc import Callable

ControlAction = Callable[[], None]
# 29단계 PR11 — `/unpause` 의 콜백은 *실제로 풀었는지* bool 을 반환해 응답 메시지 분기.
UnpauseAction = Callable[[], bool]


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


def handle_unpause(on_unpause: UnpauseAction | None) -> str:
    """29단계 PR11 — 연속 손절 자동 일시중지의 수동 해제.

    콜백이 True 를 반환하면 실제로 풀린 것 — 사용자에게 명시적으로 알림.
    False 면 "이미 해제됐거나 비활성" — 운영자가 잘못 명령했을 때 명확한 피드백.
    """
    if on_unpause is None:
        return "이 봇 인스턴스에선 /unpause 가 비활성입니다."
    if on_unpause():
        return "▶ 연속 손절 일시중지 해제 — BUY 시그널 다시 활성화."
    return "현재 연속 손절 일시중지 상태가 아닙니다 (이미 해제 또는 한도 비활성)."
