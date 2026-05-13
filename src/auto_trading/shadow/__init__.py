"""Shadow Strategy 모드 — 라이브 데이터에 신규 전략을 *주문 X* 로 평가.

29단계 PR42 (#7) — 백테스트 → 실거래 사이의 마지막 게이트.

라이브 시그널·가격을 받아 가상 PnL 만 누적. 실제 주문 송신 X.
백테스트가 *과거* 를 가정한 시뮬레이션이고 paper-day 가 *모의계좌 체결* 이라면,
shadow 는 *실시간 실데이터에 신호만 평가* — 백테스트와 라이브 괴리를 직접 측정.
"""

from auto_trading.shadow.tracker import ShadowSignal, ShadowSnapshot, ShadowTracker

__all__ = ["ShadowSignal", "ShadowSnapshot", "ShadowTracker"]
