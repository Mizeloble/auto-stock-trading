"""유니버스 종목 0B 틱 원본 JSONL 기록 (diag 전용).

2026-07-01 금호전기(001210) 손절 오버슈트 진단 후속 — 설정 -1% 손절이 -4.48% 에서
체결됐다. PositionGuard 검사가 1분봉 확정 시점(runner._on_bar 끝)에만 돌아 봉 내부
급락을 놓치기 때문. "틱 기반 가드였다면 손절가·승자 조기손절이 어땠을까" 반사실
분석에는 틱 원본이 필요한데 어디에도 저장되지 않아 검증 불가였다 — 이 모듈은 그
데이터 수집만 담당한다 (inverse_etf_study 와 같은 diag-first: 데이터 먼저,
트레이딩 로직 무변경).

설계 — monitor/ 규칙 + `price_snapshot.py` 와 동일 정신:
- 일별 회전 파일: ``data/ticks/ticks-YYYY-MM-DD.jsonl`` (`topcap-*.jsonl` 규칙 동일).
- 핫패스 보호: 매 틱 ``record()`` 는 메모리 버퍼 append 만. 디스크 write 는 버퍼
  임계(기본 200줄) 또는 flush 간격(기본 5초) 경과 시 한 번에 모아서 — 장중
  3종목 수준이면 수 초에 한 번의 append 호출로 끝난다.
- best-effort: 디스크 오류는 swallow + 로그. 실패해도 버퍼는 비워 무한 증식 방지.
- read-only: 트레이딩 상태를 읽지도 쓰지도 않는다. 입력은 `market.feed.Tick` 뿐.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import structlog

from .._common import KST
from ..config import Settings, get_settings
from ..market.feed import Tick

log = structlog.get_logger(__name__)

__all__ = [
    "TickRecorder",
    "get_default_tick_recorder",
    "tick_log_path_for",
]


def tick_log_path_for(out_dir: Path, when: date | None = None) -> Path:
    """`out_dir/ticks-YYYY-MM-DD.jsonl` — 일별 회전 파일 표준 경로."""
    d = when or datetime.now(tz=KST).date()
    return out_dir / f"ticks-{d.isoformat()}.jsonl"


@dataclass
class TickRecorder:
    """틱 → 메모리 버퍼 → 일별 JSONL 묶음 append.

    한 줄 = 틱 하나: ``{"ts": ISO8601 KST, "symbol", "price", "volume"}``.
    운영 경로는 ``data/ticks/``. flush 는 record() 안에서 임계/간격 도달 시
    자동으로 일어나고, 러너 종료 시 잔여분을 한 번 더 flush() 해야 한다.
    """

    out_dir: Path
    flush_threshold: int = 200
    flush_interval_sec: float = 5.0
    _buffer: list[str] = field(default_factory=list, init=False, repr=False)
    # 버퍼에 쌓인 틱들의 날짜 — 자정 넘김(이론상) 시 이전 날짜 파일로 먼저 비운다.
    _buffer_date: date | None = field(default=None, init=False)
    # 0.0 시작 → 첫 틱에서 즉시 flush. 파일이 장 시작과 함께 생겨 "기록 중" 확인이 쉽다.
    _last_flush: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def record(self, tick: Tick) -> None:
        """버퍼 append. 임계·간격 도달 시에만 디스크 flush — 핫패스 비용 최소."""
        tick_date = tick.ts.date()
        if self._buffer_date is not None and tick_date != self._buffer_date:
            self.flush()
        self._buffer_date = tick_date
        self._buffer.append(
            json.dumps(
                {
                    "ts": tick.ts.isoformat(),
                    "symbol": tick.symbol,
                    "price": float(tick.price),
                    "volume": tick.volume,
                },
                ensure_ascii=False,
            )
        )
        if (
            len(self._buffer) >= self.flush_threshold
            or time.monotonic() - self._last_flush >= self.flush_interval_sec
        ):
            self.flush()

    def flush(self) -> None:
        """버퍼 전체를 해당 날짜 파일에 한 번의 append 로 기록. 오류 swallow."""
        self._last_flush = time.monotonic()
        if not self._buffer:
            return
        path = tick_log_path_for(self.out_dir, self._buffer_date)
        payload = "\n".join(self._buffer) + "\n"
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(payload)
        except OSError as exc:
            log.error(
                "tick_recorder.write_failed",
                path=str(path),
                dropped_lines=len(self._buffer),
                error=str(exc),
            )
        self._buffer.clear()


def get_default_tick_recorder(settings: Settings | None = None) -> TickRecorder | None:
    """``Settings.tick_log_dir`` 기반 default 레코더. 미설정 시 None (비활성)."""
    s = settings or get_settings()
    if s.tick_log_dir is None:
        return None
    return TickRecorder(out_dir=s.tick_log_dir)
