# cli/ 규칙

- 진입점은 `__init__.py` 의 `app`(Typer) — `pyproject.toml` 의 `auto-trade` 스크립트가 여기. 명령은 도메인별로 분할: `_trading.py`(quote/balance/verify-symbol/paper/paper-day), `_backtest.py`, `_reporting.py`(report/insight/weekly-proposal/fetch-news/detect-regime), `_strategy.py`(strategy 서브앱), `_ops.py`(dashboard/preflight/heartbeat-check/latency/data/orders/vi/build-marketcap-universe/snapshot-top200). 새 명령은 맞는 파일에 `@cli.app.command()` 로 등록.
- 서브모듈 import 는 `__init__.py` **맨 끝**에서 한다 — `app`·헬퍼 정의 뒤라야 순환 import 가 안 난다. 순서를 바꾸지 말 것.
- 테스트가 `auto_trading.cli.<심볼>` 을 monkeypatch 한다(예: `cli.KiwoomRestClient`). 공개 심볼은 `__all__` 로 유지 — 이름을 바꾸면 CLI 테스트가 조용히 깨진다.
- CLI 는 **인자 파싱 + 위임만**. 매매·집계·발굴 로직을 여기 두지 말고 도메인 모듈을 호출한다(왜: 백테스트/러너와 동일 로직을 공유·테스트하기 위해).
- 프로파일 파일 로딩·상호배타 검증은 `_profile_io.py` 공유 헬퍼 사용 — 초보 사용자가 읽을 친화적 한국어 오류로 변환되어 있다. 직접 yaml 파싱 후 raw 예외를 노출하지 말 것.
