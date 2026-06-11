"""주문 영속 저널 — append-only JSONL.

29단계 PR1 (#9 주문 idempotency 1차) — 봇이 죽었을 때 "어떤 주문을 보냈는지"를
디스크에서 재구성할 수 있게 한다. 재시작 후 키움에 재조회하여 일관성 복구하는
로직(다음 PR)의 기반.

이번 PR 의 책임은 **기록만**:
- 주문 송신 직전: ``submit`` 이벤트 (client_id 부여)
- 키움 ack 수신: ``accepted`` 이벤트 (order_no 매핑)
- 송신 실패: ``failed`` 이벤트 (error 메시지)

JSONL 을 택한 이유:
- 동시성: append 만 하므로 여러 코루틴이 같은 파일에 써도 라인 단위로 안전.
- 손상 복원: 한 줄이 깨져도 다른 줄은 살아있음.
- 파싱 단순: 한 줄 = 한 이벤트.

쓰기 실패는 swallow 하고 구조화 로그만 남긴다 — 디스크 I/O 가 주문 자체를
막지 않게. 운영 중 디스크 가득 / 권한 문제는 로그/heartbeat 로 별도 감지.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

import structlog

from .._common import KST
from .orders import Order

log = structlog.get_logger(__name__)

OrderEvent = Literal["submit", "accepted", "failed", "unknown"]

__all__ = ["OrderEvent", "OrderJournal", "new_client_id"]


def new_client_id() -> str:
    """새 client UUID. uuid4 의 hex 32자 — DB / API id 와 충돌 없음."""
    return uuid4().hex


@dataclass
class OrderJournal:
    """append-only JSONL 주문 저널.

    같은 프로세스 내 여러 코루틴이 동시에 호출해도 OS 의 append-mode 가 라인
    단위 atomic 을 보장하므로 락 없이 안전 (POSIX `O_APPEND`).
    """

    path: Path

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def submit(self, *, client_id: str, order: Order) -> None:
        """주문 송신 직전. order_no 는 이 시점 미정."""
        self._append(
            client_id=client_id,
            event="submit",
            symbol=order.symbol,
            side=order.side.value,
            qty=order.quantity,
            type=order.order_type.value,
            price=str(order.price) if order.price is not None else None,
            exchange=order.exchange,
        )

    def accepted(self, *, client_id: str, order_no: str) -> None:
        """키움 응답에서 ord_no 추출 직후."""
        self._append(client_id=client_id, event="accepted", order_no=order_no)

    def failed(self, *, client_id: str, error: str) -> None:
        """송신 예외 — 키움이 처리 후 *확정 거절* 한 경우만. 메시지에서 시크릿이
        새지 않게 호출자가 사전 정제."""
        self._append(client_id=client_id, event="failed", error=error)

    def unknown(self, *, client_id: str, error: str) -> None:
        """결과 미확정 — 타임아웃/5xx 등 키움이 접수했을 *가능성* 이 있는 실패.

        `failed` 와 달리 suspect 해소로 치지 않는다 — 재시작 복구 스캔
        (`recovery.find_suspect_orders`)이 이 주문을 계속 의심 목록에 올려
        사용자/후속 대사 로직이 실제 체결 여부를 확인하게 한다.
        """
        self._append(client_id=client_id, event="unknown", error=error)

    def replay(self) -> Iterator[dict]:
        """저널 전체를 한 줄씩 dict 로 yield. 손상된 라인은 스킵하고 로그만."""
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as fh:
            for line_no, raw in enumerate(fh, start=1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    log.warning(
                        "order_journal.corrupt_line",
                        path=str(self.path),
                        line_no=line_no,
                        error=str(exc),
                    )

    def _append(self, **fields: object) -> None:
        record = {"ts": datetime.now(tz=KST).isoformat(), **fields}
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError as exc:
            log.error(
                "order_journal.write_failed",
                path=str(self.path),
                event=fields.get("event"),
                client_id=fields.get("client_id"),
                error=str(exc),
            )
