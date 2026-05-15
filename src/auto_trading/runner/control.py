"""러너 외부 제어 상태 (텔레그램 v3 /pause /resume /stop).

`runner` 패키지 분리 전 `runner.py` 에 함께 있던 enum 을 독립 모듈로 분리.
`PaperRunner` 와 무관한 순수 enum 이라 분리해도 동작·테스트 영향 없음.
"""

from __future__ import annotations

from enum import StrEnum


class RunnerControlState(StrEnum):
    """텔레그램 v3 (/pause /resume /stop) 으로 외부에서 토글 가능한 러너 제어 상태.

    - RUNNING : 정상 — 시그널·리스크·주문 전체 실행
    - PAUSED  : 시그널 기록·리스크 평가는 그대로, 실제 주문 전송만 건너뜀
                (텔레그램 /last_signal 정확도 유지하면서 일시 안전 모드)
    - STOPPING: 다음 tick 경계에서 graceful break, finally 블록으로 정리
    """

    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
