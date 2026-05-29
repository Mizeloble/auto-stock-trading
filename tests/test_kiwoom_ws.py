"""KiwoomWebSocket._run_loop 견고성 회귀 테스트.

핵심 검증 (2026 검토 #1):
- 토큰 취득/로그인/구독에서 난 RuntimeError 등 비-연결 예외가 _run_loop 밖으로
  전파되어 ws 백그라운드 태스크가 조용히 죽지 않는다. 대신 재접속 백오프를 타고
  다음 사이클로 넘어간다 — 과거엔 (ConnectionClosed, OSError) 만 잡아 장 시작 시
  토큰/로그인 실패 한 번이면 시세 태스크가 종일 침묵 정지했다.
- _stop 이 set 되면 백오프 루프가 다음 사이클에 깔끔히 빠져나온다.
"""

from __future__ import annotations

import asyncio
import types

from auto_trading.kiwoom.ws import KiwoomWebSocket


def _bare_ws(token_provider: object) -> KiwoomWebSocket:
    """네트워크/설정 의존 없이 _run_loop 만 돌릴 수 있는 최소 인스턴스."""
    ws = KiwoomWebSocket.__new__(KiwoomWebSocket)
    ws._settings = types.SimpleNamespace(ws_url="wss://example.invalid")
    ws._queue = asyncio.Queue()
    ws._conn = None
    ws._subscriptions = []
    ws._task = None
    ws._stop = asyncio.Event()
    ws._token_provider = token_provider  # type: ignore[assignment]
    return ws


async def test_run_loop_absorbs_runtime_error_and_retries(monkeypatch) -> None:
    """get_token 의 RuntimeError(인증/로그인 실패 류)가 태스크를 죽이지 않고 재시도된다."""
    calls = {"n": 0}

    class _FlakyTokenProvider:
        async def get_token(self) -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                # 1차: 장 시작 시 토큰/로그인 실패를 모사 — 과거엔 이게 태스크를 죽였다.
                raise RuntimeError("token/login failed")
            # 2차: 루프가 재진입했다는 증거를 남기고 정상 종료시킨다.
            ws._stop.set()
            raise RuntimeError("stop-after-retry")

    ws = _bare_ws(_FlakyTokenProvider())
    # 백오프 sleep 을 즉시 통과시켜 테스트를 빠르게 (원본을 먼저 잡아 재귀 방지).
    _real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda _s: _real_sleep(0))

    # 예외가 전파되면 여기서 raise 되어 실패한다. 정상이면 조용히 반환.
    await asyncio.wait_for(ws._run_loop(), timeout=3)

    # 1차 실패 후 재루프해 2차까지 호출됐다 = 태스크가 죽지 않았다.
    assert calls["n"] == 2


async def test_run_loop_exits_when_stopped(monkeypatch) -> None:
    """_stop 이 이미 set 이면 _run_loop 는 한 사이클도 안 돌고 즉시 반환."""

    class _NeverCalledTokenProvider:
        async def get_token(self) -> str:  # pragma: no cover - 호출되면 안 됨
            raise AssertionError("stop 상태에서 get_token 이 호출되면 안 된다")

    ws = _bare_ws(_NeverCalledTokenProvider())
    ws._stop.set()
    _real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda _s: _real_sleep(0))

    await asyncio.wait_for(ws._run_loop(), timeout=3)
