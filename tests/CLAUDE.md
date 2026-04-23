# tests/ 규칙

- 네트워크 호출은 `respx`(HTTP) 또는 로컬 페이크 WS 로 대체. 실 API 호출은 `@pytest.mark.integration` 로만.
- `.env` 의존 금지 — 픽스처에서 `Settings` 를 직접 주입하거나 monkeypatch.
- async 테스트는 `asyncio_mode = "auto"` 라 별도 데코레이터 없이 `async def test_…` 작성 가능.
