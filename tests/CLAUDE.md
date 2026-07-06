# tests/ 규칙

- 네트워크 호출은 `respx`(HTTP) 또는 로컬 페이크 WS 로 대체. 실 API 호출은 `@pytest.mark.integration` 로만.
- `.env` 의존 금지 — 픽스처에서 `Settings` 를 직접 주입하거나 monkeypatch.
- async 테스트는 `asyncio_mode = "auto"` 라 별도 데코레이터 없이 `async def test_…` 작성 가능.
- 통합 테스트는 세션 스코프 이벤트 루프 공유(`asyncio_default_fixture_loop_scope = "session"`) — 키움 토큰 발급 rate limit 을 피하려고 토큰을 세션 내 1회만 받기 위함. 이 스코프를 바꾸지 말 것. 기본 실행은 `-m 'not integration'` 으로 단위만 돈다.
- `conftest.py` 격리 픽스처: `_isolate_token_cache` 는 autouse(자동), **`isolated_cwd` 는 옵트인** — cwd 상대 경로 파일(`data/`·`logs/`)을 만들 수 있는 코드(예: `delayed_discover`)를 지나는 테스트는 반드시 인자로 받아라. 빠뜨리면 pytest 가 운영 데이터 파일을 오염시킨다(2026-07-05 `alt-rank.jsonl` 오염 사고).
- CLI 테스트는 명령별 `test_cli_*.py` 로 분리하고 `auto_trading.cli.<심볼>` 을 monkeypatch 한다(예: `cli.KiwoomRestClient`) — cli/ 의 `__all__` 공개 심볼 계약에 의존.
