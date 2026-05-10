"""연속 손절 자동 일시중지.

29단계 PR9 (#14 1차) — 라운드트립 PnL 이 N번 연속 음수면 봇 신규 진입을
일시 차단. 시장 레짐이 갑자기 바뀐 신호일 가능성 — 그대로 두면 4번째·5번째
손절. "잠시 멈춰서 재평가" 가 가장 단순한 방어.

이번 PR 의 책임은 *순수 상태 머신*: 외부 호출 없이 라운드트립 PnL 시퀀스만
보고 paused 여부를 결정. runner 통합 (라운드트립 PnL 산출 + evaluate 결합)
은 PR10 으로 분리.

운영 default 는 비활성 (``max_consecutive_losses=0``). 명시 활성 후에만
작동.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .._common import KST

__all__ = ["ConsecutiveLossTracker"]


@dataclass
class ConsecutiveLossTracker:
    """라운드트립 손절 카운터 + 자동/수동 일시중지.

    호출자는 매 SELL 청산 후 ``record_round_trip(pnl)`` 으로 결과 알림.
    ``is_paused(now)`` 가 True 인 동안 RiskManager 가 신규 진입을 거절.

    상태 변경 흐름:
    - pnl < 0: ``streak`` 증가. ``streak >= threshold`` 도달 시 paused 시작.
    - pnl >= 0: ``streak`` reset. paused 는 *유지* (자동 만료 또는 수동 unpause).
    - 만료된 ``paused_until`` 은 ``is_paused`` 호출 시 자동 청소.

    threshold 가 0 이면 비활성 — record / is_paused 모두 no-op.
    """

    threshold: int = 0  # 0 이면 비활성. 운영에선 명시 활성(예: 3) 필요.
    pause_seconds: int = 86_400  # 24h
    streak: int = field(default=0, init=False)
    paused_until: datetime | None = field(default=None, init=False)

    def record_round_trip(self, pnl: float, *, now: datetime | None = None) -> None:
        """매 SELL 청산 후 호출. pnl < 0 이면 streak 증가, ≥ 0 이면 reset."""
        if self.threshold <= 0:
            return
        if pnl < 0:
            self.streak += 1
            if self.streak >= self.threshold:
                self.paused_until = (now or datetime.now(tz=KST)) + timedelta(
                    seconds=self.pause_seconds
                )
        else:
            self.streak = 0

    def is_paused(self, *, now: datetime | None = None) -> bool:
        """현재 paused 상태인지. 만료된 paused_until 은 즉시 청소."""
        if self.threshold <= 0 or self.paused_until is None:
            return False
        current = now or datetime.now(tz=KST)
        if current >= self.paused_until:
            self.paused_until = None
            self.streak = 0
            return False
        return True

    def unpause(self) -> None:
        """수동 해제 — 텔레그램 명령 등에서 호출. streak 도 같이 reset."""
        self.paused_until = None
        self.streak = 0
