"""monitor.tick_recorder 단위 회귀.

2026-07-02 diag — 금호전기(001210) 손절 오버슈트(-1% 설정 → -4.48% 체결) 후속.
틱 기반 PositionGuard 반사실 분석용 틱 원본 수집 모듈 + runner 배선 검증.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from auto_trading._common import KST
from auto_trading.config import KiwoomMode, Settings
from auto_trading.market.feed import Tick
from auto_trading.monitor.tick_recorder import (
    TickRecorder,
    get_default_tick_recorder,
    tick_log_path_for,
)
from auto_trading.runner import PaperRunner
from auto_trading.strategy.sma_crossover import SmaCrossover


def _tick(
    *,
    symbol: str = "001210",
    ts: datetime | None = None,
    price: str = "1234",
    volume: int = 10,
) -> Tick:
    return Tick(
        symbol=symbol,
        ts=ts or datetime(2026, 7, 2, 9, 10, 58, tzinfo=KST),
        price=Decimal(price),
        volume=volume,
    )


def test_path_is_daily_rotated(tmp_path: Path) -> None:
    d = datetime(2026, 7, 2, tzinfo=KST).date()
    assert tick_log_path_for(tmp_path, d) == tmp_path / "ticks-2026-07-02.jsonl"


def test_record_buffers_without_disk_write(tmp_path: Path) -> None:
    rec = TickRecorder(out_dir=tmp_path, flush_threshold=100, flush_interval_sec=3600)
    rec._last_flush = 10**12  # 간격 flush 억제 — 순수 버퍼링 검증
    rec.record(_tick())
    rec.record(_tick(price="1235"))
    assert len(rec._buffer) == 2
    assert list(tmp_path.glob("*.jsonl")) == []


def test_flush_writes_parseable_jsonl(tmp_path: Path) -> None:
    rec = TickRecorder(out_dir=tmp_path, flush_threshold=100, flush_interval_sec=3600)
    rec._last_flush = 10**12
    ts = datetime(2026, 7, 2, 9, 9, 58, tzinfo=KST)
    rec.record(_tick(ts=ts, price="4470", volume=33))
    rec.flush()
    lines = (tmp_path / "ticks-2026-07-02.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data == {
        "ts": ts.isoformat(),
        "symbol": "001210",
        "price": 4470.0,
        "volume": 33,
    }
    assert rec._buffer == []


def test_threshold_triggers_auto_flush(tmp_path: Path) -> None:
    rec = TickRecorder(out_dir=tmp_path, flush_threshold=3, flush_interval_sec=3600)
    rec._last_flush = 10**12
    for i in range(3):
        rec.record(_tick(price=str(1000 + i)))
    lines = (tmp_path / "ticks-2026-07-02.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert rec._buffer == []


def test_interval_triggers_flush_on_first_record(tmp_path: Path) -> None:
    # _last_flush=0.0 시작 → 첫 틱에서 간격 초과로 즉시 flush (파일이 장 시작과 함께 생김).
    rec = TickRecorder(out_dir=tmp_path, flush_threshold=1000)
    rec.record(_tick())
    assert (tmp_path / "ticks-2026-07-02.jsonl").exists()


def test_date_rollover_writes_to_separate_files(tmp_path: Path) -> None:
    rec = TickRecorder(out_dir=tmp_path, flush_threshold=100, flush_interval_sec=3600)
    rec._last_flush = 10**12
    rec.record(_tick(ts=datetime(2026, 7, 2, 15, 30, tzinfo=KST)))
    rec.record(_tick(ts=datetime(2026, 7, 3, 9, 0, tzinfo=KST)))  # 날짜 변경 → 이전분 flush
    rec.flush()
    assert (tmp_path / "ticks-2026-07-02.jsonl").exists()
    assert (tmp_path / "ticks-2026-07-03.jsonl").exists()


def test_flush_swallows_disk_failure_and_clears_buffer(tmp_path: Path) -> None:
    rec = TickRecorder(out_dir=tmp_path, flush_threshold=100, flush_interval_sec=3600)
    rec._last_flush = 10**12
    rec.record(_tick())
    with patch.object(Path, "open", side_effect=OSError("disk full")):
        rec.flush()  # 예외 X
    assert rec._buffer == []  # 무한 증식 방지 — 실패해도 버퍼는 비운다


def test_flush_noop_when_buffer_empty(tmp_path: Path) -> None:
    rec = TickRecorder(out_dir=tmp_path)
    rec.flush()
    assert list(tmp_path.glob("*.jsonl")) == []


def test_get_default_recorder_returns_none_when_unset() -> None:
    s = Settings(
        kiwoom_mode=KiwoomMode.MOCK,
        kiwoom_app_key="k",
        kiwoom_app_secret="s",
        tick_log_dir=None,
    )
    assert get_default_tick_recorder(s) is None


def test_get_default_recorder_returns_recorder_when_dir_set(tmp_path: Path) -> None:
    s = Settings(
        kiwoom_mode=KiwoomMode.MOCK,
        kiwoom_app_key="k",
        kiwoom_app_secret="s",
        tick_log_dir=tmp_path / "ticks",
    )
    rec = get_default_tick_recorder(s)
    assert rec is not None
    assert rec.out_dir == tmp_path / "ticks"


def test_runner_auto_creates_recorder_from_settings(tmp_path: Path) -> None:
    s = Settings(
        kiwoom_mode=KiwoomMode.MOCK,
        kiwoom_app_key="k",
        kiwoom_app_secret="s",
        tick_log_dir=tmp_path / "ticks",
    )
    r = PaperRunner(strategy=SmaCrossover(), symbols=["005930"], _settings=s)
    assert r.tick_recorder is not None
    assert r.tick_recorder.out_dir == tmp_path / "ticks"


def test_runner_recorder_none_when_settings_unset() -> None:
    s = Settings(
        kiwoom_mode=KiwoomMode.MOCK,
        kiwoom_app_key="k",
        kiwoom_app_secret="s",
        tick_log_dir=None,
    )
    r = PaperRunner(strategy=SmaCrossover(), symbols=["005930"], _settings=s)
    assert r.tick_recorder is None
