# 진행 기록 (1~26단계 완료 아카이브)

`todo-list.md` 가 비대해져 완료된 단계들을 별도 파일로 이관. 각 단계의 머지 PR 번호와 날짜를 보존하므로 회고/검증/사이드픽스 추적은 이 파일 + `git log` 로 가능하다.

> 새 작업은 `todo-list.md` 에서 추적. 끝난 단계만 아래에 누적.

---

## 📦 1단계 · 프로젝트 부트스트랩 — **✅ 완료**

- [x] **uv 프로젝트 초기화** (Python 3.14)
- [x] **의존성 설치** (httpx/websockets/pandas/pydantic-settings/structlog/typer + pytest)
- [x] **`.env.example` / `.gitignore`** · 시크릿 분리 + git 제외
- [x] **모의투자 키 받기** (사용자 작업!) — 2026-04-25 완료
  - 주의: 키움은 실전/모의 AppKey 가 **별도 발급**. 같은 키로 base URL 만 바꾸려면 모의투자 신청 *이후* 발급된 키여야 함. 8030 에러 만나면 키 재발급 필요.
  - 확인: `uv run auto-trade quote 005930` 에서 현재가가 표로 나오면 성공.

---

## 🔐 2단계 · OAuth 토큰 인증 — **✅ 완료**

- [x] **`kiwoom/auth.py`** · AppKey+Secret → 24h 토큰 발급 · 만료 10분 전 선제 재발급 · 메모리 캐시
- [x] **단위 테스트 4종** (`tests/test_auth.py`) — 발급/캐시/재발급/에러 매핑

---

## 🌐 3단계 · REST 클라이언트 — **✅ 완료**

- [x] **`kiwoom/rest.py`** · TR 헤더 주입, 429/5xx 지수 백오프, 401 재토큰, 페이지네이션
- [x] **`kiwoom/endpoints.py`** · 시세/차트/계좌/주문 TR 상수화
- [x] **`market/quote.py`, `market/chart.py`** · 현재가·일봉 조회 도메인 어댑터
- [x] **`account/balance.py`, `account/orders.py`** · 예수금·잔고·매수/매도/취소 래퍼
- [x] **`cli.py quote / balance`** — `uv run auto-trade quote 005930` / `uv run auto-trade balance`
- [x] **REST 단위 테스트 5종** (`tests/test_rest_client.py`)

### 체험 확인
- [x] `.env` 작성 후 `uv run auto-trade quote 005930` 실행 → 현재가가 표로 출력되면 완료. (2026-04-25)

---

## 📡 4단계 · WebSocket 실시간 시세 — **✅ 완료**

- [x] **`kiwoom/ws.py`** · LOGIN → REG 구독 → PING 에코, 자동 재접속(지수 백오프)
- [x] **`market/feed.py`** · WS 원본 메시지를 `Tick` 도메인 객체로 변환

> WS 통합 테스트는 실 모의투자 키가 있을 때 `pytest -m integration` 로 돌립니다(추후 추가).

---

## 🧠 5단계 · 전략 엔진 — **✅ 완료**

- [x] **`strategy/base.py`** · `Strategy` ABC, `Bar` · `Signal` 도메인 타입
- [x] **`strategy/registry.py`** · `@register("name")` 플러그인 등록
- [x] **`strategy/sma_crossover.py`** · 이동평균 골든/데드크로스 예제 전략
- [x] **테스트 4종** — 골든/데드크로스 / 워밍업 / 레지스트리 / reset

---

## 📊 6단계 · 백테스트 엔진 — **✅ 완료**

- [x] **`backtest/broker.py`** · 수수료 0.015% / 매도세 0.18% / 슬리피지 훅
- [x] **`backtest/engine.py`** · 다음 봉 시가 체결(look-ahead 방지), equity curve 기록, CSV/JSON 저장
- [x] **`backtest/metrics.py`** · 총수익률·CAGR·Sharpe·MDD·승률
- [x] **`cli.py backtest`** — `uv run auto-trade backtest -s sma_crossover --symbol 005930 --from 2024-01-01`
- [x] **테스트 4종** — 체결 타이밍 / 상승장 수익 / 결정론성 / 현금 부족 거절

### 체험 확인
- [x] `.env` 설정 후 위 명령을 실행 → `data/backtests/…/report.json` + `trades.csv` + `equity.csv` 가 쌓이면 완료. (2026-04-25 005930, 2024-01-01~ 563봉 → 총수익률 103.63%, Sharpe 1.169)

---

## 🛡 7단계 · 리스크 매니저 — **✅ 완료**

- [x] **`risk/manager.py`** · 종목당 20% / 포트폴리오 80% / 일일 손실 한도 3% / 최소 주문금액
- [x] **테스트 6종** — 비중/손실한도/최소주문/매도·매수 조합

---

## ▶️ 8단계 · 모의투자 라이브 러너 — **✅ 완료**

- [x] **`runner.py PaperRunner`** · WS 틱 → 분봉 집계 → 전략 → 리스크 → 모의 주문 → 로그
- [x] **실거래 가드** · `KIWOOM_MODE=real` + `--allow-real` 둘 다 없으면 실행 거부
- [x] **BarAggregator 테스트 2종** — 1분봉 OHLC 정확성, 5분봉 버킷 경계

### 체험 확인
- [x] 모의 키 세팅 후 **장중 시간** 에 `uv run auto-trade paper -s sma_crossover --symbols 005930` 실행 → 로그에 `ws.connected`, `ws.subscribe`, `runner.bar` 가 쌓이면 정상. (2026-04-29 launchd 09:00 자동 시동 + 다중 종목(005930+000660) sma_crossover 1회 검증 — `runner.order_simulated` 005930 buy 44주·000660 buy 7주 흐름 확인. `logs/paper-2026-04-29.log`)

---

## ⌨️ 9단계 · CLI 정리 — **✅ 완료**

- [x] `quote`, `balance`, `backtest`, `paper` 네 명령 전부 `auto-trade` 하위로 노출
- [x] `uv run auto-trade --help` 에서 전체 명령 확인 가능
- [x] 모드 배너(mock=초록, real=빨강) — 실거래 착각 방지

---

## ✅ 10단계 · 테스트 요약 — **✅ 완료 (단위 71 + 통합 4 = 75건 passing)**

- `tests/test_auth.py` (5) · 비즈니스 에러(`return_code≠0`) 매핑 회귀 추가
- `tests/test_market_chart.py` (2) · 일봉 TR/path/필드명 회귀 (PR #2)
- `tests/test_market_quote.py` (2) · 현재가 TR/path/필드명 회귀 (`ka10001`+`stkinfo`, 가격 부호 처리)
- `tests/test_account_orders.py` (6) · 매수/매도/취소 body·TR 검증 + `ord_no`→`odno` fallback + RC4010 거절 회귀
- `tests/test_notify_telegram.py` (3) · sendMessage body·실패 swallow·토큰 마스킹
- `tests/test_notify_factory.py` (2) · 키 유무 → Telegram/Noop 폴백
- `tests/test_rest_client.py` (5)
- `tests/test_common.py` (7)
- `tests/test_strategy_sma.py` (4)
- `tests/test_strategy_rsi.py` (5) · RSI 전략
- `tests/test_backtest_engine.py` (4)
- `tests/test_risk_manager.py` (7)
- `tests/test_runner_aggregator.py` (2)
- `tests/test_runner.py` (8) · 잔고 폴백·dry-run + notifier hook 2건
- `tests/test_ws_subscribe.py` (3) · 재구독 재시도
- `tests/test_cli_parsing.py` (6) · 종목별 파라미터 파서
- `tests/integration/test_rest_roundtrip.py` (3, 키 있을 때만) · quote · daily_bars · balance shape
- `tests/integration/test_orders_roundtrip.py` (1, 키 있을 때만) · 취소 거절 흐름이 KiwoomError 로 surface

```
uv run pytest                  # 단위 71건 (네트워크 없음)
uv run pytest -m integration   # 통합 4건 (모의투자 키 있을 때만)
```

---

## 🛡 11단계 · 운영 안정화 개선 — **✅ 완료**

- [x] **러너 잔고 폴백 로직** · `_snapshot_account` 가 실패 시 직전 상태 유지 + `max_balance_failures` 초과 시 러너 중단 (`src/auto_trading/runner.py`)
- [x] **WS 구독 재시도** · 구독 실패 시 지수 백오프 재시도, 연결 끊김은 재접속 루프에 위임 (`src/auto_trading/kiwoom/ws.py`)
- [x] **리스크 매니저 Decimal 경계** · 예산/수량 산식을 모두 `Decimal` 로 통일해 FP 반올림 오차 제거 (`src/auto_trading/risk/manager.py`)

## 🧰 12단계 · 품질 게이트 — **✅ 완료**

- [x] **ruff + mypy 도입** · `pyproject.toml` 에 lint/format/타입체크 설정 + dev 의존성 추가
- [x] **GitHub Actions CI** · `.github/workflows/ci.yml` — push/PR 에서 ruff/mypy/pytest 자동 실행
- [x] **통합 테스트 프레임** · `tests/integration/` + `@pytest.mark.integration`, 키 없으면 자동 skip
- [x] **로깅 강화** · `format_exc_info` 프로세서 + 주요 에러에 `exc_info` 첨부

## ⚡️ 13단계 · 사용성 — **✅ 완료**

- [x] **`paper --dry-run` 플래그** · 모의투자에서도 실제 주문 전송 없이 `order.simulated` 로깅만
- [x] **종목별 전략 파라미터** · `--symbols 005930:short=3:long=15,000660:short=5:long=20`
- [x] **RSI 역추세 전략** · `strategy/rsi_reversion.py` + 레지스트리 등록 + 5건 테스트
- [x] **rich 의존성 명시** · `pyproject.toml` dependencies 에 추가 (전이 의존에서 직접 의존으로)

---

## 🔧 14단계 · 실 모의투자 응답 검증 — **✅ 완료**

- [x] **`market/quote.py`** · TR/path 확정 (`ka10001` + `/api/dostk/stkinfo`). 005930 정상 응답 확인 (219,500 / -2.23%). 회귀 테스트 2건 추가 (`tests/test_market_quote.py`).
- [x] **`kiwoom/auth.py`** · HTTP 200 + `return_code≠0` 비즈니스 에러를 `KiwoomError` 로 매핑 (이전엔 ValidationError 로 폭주). 회귀 테스트 1건 추가.
- [x] **`account/balance.py`** · `uv run auto-trade balance` 동작 확인 (예수금 5천만원 표시, 페이지네이션 정상).
- [x] **`market/chart.py`** · `ka10081` + `/api/dostk/chart` mock 실측 정상 확인. 회귀 테스트 2건 추가 (PR #2). 코드 변경 불필요.
- [x] **통합 테스트 인프라** · 토큰 endpoint(au10001) 분당 호출 제한을 피하기 위해 `tests/integration/conftest.py` 에 session 스코프 `kiwoom_client` 픽스처 추가. `pyproject.toml` 의 `asyncio_default_*_loop_scope = "session"` 도입. `uv run pytest -m integration` → 3건 모두 통과 (quote / daily_bars / balance shape).
- [x] **`account/orders.py` 응답 매핑 안전망 + 거절 플로우 검증** (2026-04-25, 토요일).
  - `scripts/probe_orders.py` 추가 — 토큰만 받아 raw POST 로 응답 dict 캡처 (KiwoomError 우회용 진단 도구).
  - 모의서버 실측 (영업일 아님): BUY/SELL/CANCEL 모두 `{"return_code": 20, "return_msg": "[2000](RC401x:...)"}` 만 응답. HTTP 200 + return_code≠0.
  - `tests/test_account_orders.py` 추가 (6건) — 시장가/지정가 body·TR 검증, `ord_no`→`odno` fallback, 둘 다 없을 때 빈 문자열, RC4010 거절이 KiwoomError 로 매핑, 취소.
  - `tests/integration/test_orders_roundtrip.py` 추가 (1건) — 가짜 원주문번호로 취소 요청 → 키움 거절 → KiwoomError 로 surface (주말/평일 일관 통과).
  - 결정: 성공 응답의 정확한 주문번호 키(`ord_no` vs `odno`) 확정은 보류. 현재 fallback 체인이 양쪽을 모두 잡으므로 어떤 키여도 정상 동작 — `paper` 첫 실행 시 `order.placed` 로그로 자연 검증되며, 그때 체인을 단순화한다.

---

## 📲 15단계 · 알림 (텔레그램) v1 — **✅ 완료** (2026-04-25)

- [x] **`notify/` 패키지** · `Notifier` 프로토콜 + `NoopNotifier` (폴백) + `TelegramNotifier` + `build_notifier()` 팩토리. 토큰은 `__repr__` 마스킹.
- [x] **`config.Settings`** · `telegram_bot_token` / `telegram_chat_id` 옵셔널 + `has_telegram` 프로퍼티. `.env.example` 안내 추가.
- [x] **`PaperRunner` hook 5개** · started / order_accepted / order_failed / balance_failed_fatal / stopped. 알림 실패는 swallow (트레이딩 핫패스 보호).
- [x] **`cli.py paper`** · `build_notifier()` 자동 주입. 텔레그램 키 없으면 `NoopNotifier` 폴백 — 기존 동작 그대로.
- [x] **테스트 7건 신규** — `test_notify_factory.py`(2) + `test_notify_telegram.py`(3, sendMessage body·실패 swallow·토큰 마스킹) + `test_runner.py`(2, order_accepted hook·balance_failed_fatal hook).
- [x] **품질 게이트** · 단위 71건 / 통합 4건 / ruff / ruff-format / mypy 전부 클린.

### 체험 확인
- [ ] 모바일 텔레그램 푸시 검증 — BotFather 봇 만들고 `.env` 에 `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` 채운 뒤 `uv run auto-trade paper -s sma_crossover --symbols 005930 --dry-run` 실행 → "▶️ paper 시작" 푸시 도착하면 성공.

### v3 후속 (구현 안 함)
- v3: `/pause` `/resume` `/stop` (러너 상태 머신).
- 옵션: 슬랙·디스코드 백엔드 (Notifier 프로토콜 그대로).

---

## 🤖 16단계 · 텔레그램 v2 (인커밍 명령) — **✅ 완료** (2026-04-26)

- [x] **`config.Settings`** · `telegram_allowed_chat_ids` (콤마 구분) + `parsed_allowed_chat_ids` / `has_telegram_commands` 프로퍼티. 화이트리스트 비어있으면 폴링 자동 비활성.
- [x] **`notify/state.py`** · `RunnerStateSnapshot` (raw `AccountSnapshot` + derived `AccountState` + 종목별 최신 `Signal`) + 한국어 포맷터 5종 (`format_status` / `format_balance` / `format_positions` / `format_pnl` / `format_last_signal`).
- [x] **`runner.py`** · `_snapshot` / `_account` / `_recent_signals` / `_started_at` 인스턴스 필드 + `state_snapshot()` 메서드. `_on_bar` 에서 시그널을 리스크 거절 여부와 무관하게 이력 기록.
- [x] **`notify/telegram_commands.py`** · `TelegramCommandHandler` long-poll 루프 (`getUpdates?timeout=30`, `update_id` offset 추적). 화이트리스트 미통과 chat 은 응답 없이 무시 (`notify.unauthorized` 로그). 모든 예외 swallow.
- [x] **`notify/factory.py`** · `build_command_handler(state_provider)` 추가. `has_telegram_commands` False → `None` 리턴 (기존 `build_notifier` 동작과 분리).
- [x] **`cli.py paper`** · `build_command_handler` 가 None 이면 기존 동작, 있으면 `asyncio.wait` 로 러너 + 폴링 task 동시 실행 + 한쪽 종료 시 cancel + aclose.
- [x] **`.env.example`** · `TELEGRAM_ALLOWED_CHAT_IDS` 라인 + chat_id 확인 방법 안내.
- [x] **테스트 24건 신규** (`test_runner_state.py` 5 + `test_telegram_commands.py` 19) — 포맷터 9종, dispatch 권한/sendMessage 4종, polling offset 2종, 인스턴스 가드 3종, runner state 5종.
- [x] **품질 게이트** · 단위 71 → 95건 (+24) / ruff / ruff-format / mypy 전부 클린.
- [x] **사이드 픽스** · `tests/test_notify_factory.py` 가 사용자 로컬 `.env` 의 텔레그램 키를 읽어들이던 누수 (`tests/CLAUDE.md` "`.env` 의존 금지" 규칙 위반)를 `_env_file=None` + 명시 빈값으로 차단.

### 체험 확인
- [ ] 모바일 텔레그램에서 양방향 명령 검증 — `.env` 에 `TELEGRAM_ALLOWED_CHAT_IDS=<본인 chat_id>` 채운 뒤 `uv run auto-trade paper -s sma_crossover --symbols 005930 --dry-run` → 봇에 `/status` `/balance` `/positions` `/pnl` `/last_signal` 보내면 응답 도착, 화이트리스트 외 chat 은 무응답.

---

## 🧠 17단계 · 초보자용 전략 추천·프로파일 워크플로 — **✅ 완료** (2026-04-26)

코드를 거의 안 짜도 되는 흐름: **추천 → YAML 저장 → 메모장 편집 → 실행**. 주기적으로 Claude API 가 방향성을 조정.

### Phase A — 추천 + YAML 프로파일

- [x] **`strategy/indicators.py`** · 순수함수 인디케이터 헬퍼 (sma/ema/rsi_wilder/atr/bollinger/cross_above·below + rsi_wilder_seed·step). 부작용 없음.
- [x] **`sma_crossover.py` / `rsi_reversion.py` 리팩터링** · indicators 호출로 위임. 기존 9건 단위테스트 무수정 통과 확인 (회귀 게이트).
- [x] **`strategy/scanner.py`** · 일봉 DataFrame → MarketRegime(추세상/추세하/박스/고변동성). 기울기·ATR%·밴드폭 휴리스틱, 임계값 모듈 상수.
- [x] **`strategy/profile.py`** · pydantic `StrategyProfile` (name/symbol/strategy/params/weight/notes/created_at/last_advised_at). `dump_yaml` 가 한국어 헤더 주석 포함, `load_yaml` 가 라운드트립.
- [x] **`strategy/suggest.py`** · 정적 PRESETS 매핑 (각 regime → StrategyPreset). `recommend(report, symbol, now=...)` 결정론.
- [x] **`cli.py strategy suggest`** · `auto-trade strategy suggest --symbol 005930` → 일봉 조회 → 분석 → `strategies/profiles/auto_<symbol>.yaml` 저장 + Rich 표.
- [x] **`backtest`/`paper` 에 `--profile`** · YAML 로드 → `registry.get(...)` 으로 인스턴스화. 기존 `--strategy/--symbol(s)` 와 mutually exclusive.
- [x] **PyYAML + types-PyYAML** 의존성 추가, `strategies/profiles/.gitkeep` 생성.
- [x] **테스트 27건 신규** — indicators(11) / scanner(5) / profile_yaml(5) / suggest(5) / cli_profile(3, 한국어 줄바꿈 정규화 + structlog 리셋 픽스처). 기존 9건 SMA/RSI 테스트 무수정 통과.

### Phase B — Claude API 방향성 조정

- [x] **`config.Settings`** · `anthropic_api_key`(SecretStr) / `anthropic_model`(기본 `claude-opus-4-7`) / `has_anthropic` 프로퍼티. `.env.example` 안내 추가.
- [x] **`strategy/llm_advisor.py`** · pydantic `AdvisorChange`/`AdvisorResponse`. `advise(profile, df, report, *, client, model)`. system 프롬프트 캐싱(`cache_control: ephemeral`) + `propose_change` tool_use 강제 + OHLC 풀시리즈 대신 요약(min/max/mean/std) 전송 → 호출당 입력 토큰 1k 미만 목표.
- [x] **`cli.py strategy advise`** · `--profile <yaml> [--dry-run]`. 키 없으면 `Exit(1)`. 변경 noop 면 백업·갱신 둘 다 skip. 변경 있으면 `<path>.bak` 백업 후 `last_advised_at` 갱신.
- [x] **`anthropic` SDK** 의존성 추가 (Phase B 만 사용, Phase A 는 키 없어도 동작).
- [x] **테스트 9건 신규** — `test_llm_advisor.py`(5: tool_use 파싱 / system 캐시 검증 / dict block 호환 / no-tool-use 거절 / weight 범위 ValidationError) + `test_cli_advise.py`(4: dry-run 무수정 / 백업+갱신 / no-change skip / 키 없음 Exit(1)).

### 품질 게이트 (2026-04-26)

- 단위 95 → **133건** (+38). 통합 4건 그대로 통과.
- `uv run ruff check` / `ruff format --check` / `mypy` 전부 클린.

### 체험 확인

- [x] 모의투자 키 + 일봉 가능 시: `uv run auto-trade strategy suggest --symbol 005930` → `strategies/profiles/auto_005930.yaml` 생성 → 메모장으로 RSI period 만 16 → 13 으로 바꿔보기 → `uv run auto-trade backtest --profile strategies/profiles/auto_005930.yaml --from 2025-01-01` 실행 → 결과 차이 확인. (2026-04-27 검증: 시장국면=high_vol → rsi_reversion period=7/oversold=25/overbought=75/weight=0.5 추천. period 7 → 5 편집 후 320봉 백테스트에서 `total_return_pct` 11.36% → 75.34%, `sharpe` 0.878 → 2.598, `num_trades` 2 → 12 로 명확히 변동 — 프로파일 → 전략 인스턴스화 경로 정상.)
- [x] (Phase B) `ANTHROPIC_API_KEY` 채운 뒤 `uv run auto-trade strategy advise --profile … --dry-run` → Advisor 제안 표 출력 + 파일 무수정 확인. (2026-04-27 검증: claude-opus-4-7 응답으로 params 5/25/75 → 7/20/80, weight 0.5 → 0.4, confidence 0.70. dry-run 무수정 후 정식 실행 → `auto_005930.yaml.bak` 백업 + `last_advised_at` + `notes`에 `[advisor] …` 갱신까지 정상.)

### 사이드 픽스 (2026-04-27)

- [x] **`config.Settings`** · `env_ignore_empty=True` 추가. 일부 호스트 환경에서 `ANTHROPIC_API_KEY=""` 가 미리 export 되어 `.env` 의 실제 키를 덮어쓰던 문제 방지. 단위 133 / ruff / mypy 클린 유지.

---

## 🤖 18단계 · 텔레그램 v3 (원격 제어) — **✅ 완료** (2026-04-28)

회사에서 폰으로 paper 러너를 일시정지/재개/종료할 수 있게 — 자동 운영 중 이상 동작 발견 시 즉시 안전 모드.

- [x] **`runner.RunnerControlState`** · `RUNNING`/`PAUSED`/`STOPPING` StrEnum. PAUSED 는 시그널·리스크 평가는 그대로 하고 `place_order` 만 건너뜀(텔레그램 `/last_signal` 정확도 유지). STOPPING 은 다음 tick 경계에서 graceful break, finally 가 ws.stop + notifier "runner.stopped" 까지 마무리. STOPPING 진입 후 pause/resume 무시(되돌릴 수 없게).
- [x] **`PaperRunner.pause()` / `resume()` / `request_stop()`** · 동기 메서드. async 루프가 매 tick 폴링하는 패턴이라 락·이벤트 불필요.
- [x] **`TelegramCommandHandler`** · `on_pause`/`on_resume`/`on_stop` 옵셔널 콜백 3개 추가. 미주입 시 "이 봇 인스턴스에선 … 비활성" 응답(기존 v2 호환). `/pause` `/resume` `/stop` dispatch + `HELP_TEXT` 갱신.
- [x] **`format_status`** · `control_state` 한국어 라벨(`▶ 운영중` / `⏸ 일시정지` / `⏹ 종료중`) 노출. RunnerStateSnapshot 에 `control_state: str = "running"` 필드 추가(기본값으로 v2 사용처 호환).
- [x] **`build_command_handler`** · 콜백 3개 keyword-only 인자로 전달. `cli.py paper` 가 `runner.pause`/`runner.resume`/`runner.request_stop` 그대로 바인딩.
- [x] **테스트 11건 신규** — runner_state(5: pause skip / running 비교군 / resume / stop terminal / snapshot value) + telegram_commands(6: dispatch 3건 / 비활성 응답 / status 라벨 / HELP_TEXT 회귀).
- [x] **품질 게이트** · 단위 133 → 144건 (+11) / ruff / ruff-format / mypy 전부 클린.

## 🖥 19단계 · 맥북 launchd 자동 운영 — **✅ 완료** (2026-04-28)

집 맥북에서 평일 장중에만 paper 러너 자동 실행 + 회사에선 텔레그램 v2/v3 로 모니터링·제어. 며칠~몇 주 굴려보고 가치 확인되면 클라우드(Oracle 무료 티어 등)로 졸업.

- [x] **`scripts/paper-day.sh`** · 평일 08:55 KST 트리거. `caffeinate -i` 로 시스템 sleep 차단 후 `uv run auto-trade paper --profile … --dry-run` 실행. 표준출력 → `logs/paper-YYYY-MM-DD.log`.
- [x] **`scripts/paper-stop.sh`** · 평일 15:35 KST 트리거. `pkill -TERM -f "auto-trade paper"` 로 SIGTERM 만 — 러너 finally 블록(ws.stop + notifier "runner.stopped") 까지 정상 동작.
- [x] **`scripts/launchd/com.mizeloble.auto-trade.paper.plist`** + **paper-stop.plist** · `StartCalendarInterval` 로 월~금 (Weekday 1~5) 트리거. `RunAtLoad=false` / `KeepAlive=false` (장 마감 종료를 비정상 죽음으로 오인해 재시동하지 않게).
- [x] **`scripts/launchd-install.sh` / `launchd-uninstall.sh`** · `launchctl bootstrap`/`bootout` 으로 등록·해제 자동화. 기존 등록 자동 정리 후 재등록.
- [x] **`scripts/launchd/README.md`** · 한국어 운영 가이드 (사전 준비·등록·강제 시동 테스트·흔한 막힘).
- [x] **시동 검증 (2026-04-27)** · `paper-day.sh` 백그라운드 → `paper-stop.sh` 로 정리 흐름 e2e 통과. 텔레그램 시작 푸시 200 OK / 키움 토큰·잔고·`ws.subscribe`·`ws.connected` 로그 정상. 8단계 체험 확인 기준(`ws.connected`/`ws.subscribe`) 도 함께 충족.

### 체험 확인

- [x] **첫 등록**: `./scripts/launchd-install.sh` → `launchctl list | grep mizeloble.auto-trade` 두 줄 보이면 성공. (4/27 시동 검증 시 확인)
- [x] **강제 즉시 시동(장 외)**: `launchctl kickstart -k gui/$UID/com.mizeloble.auto-trade.paper` → 텔레그램 시작 푸시 도착하면 시동 OK. `kickstart -k …paper-stop` 으로 정리. (4/27 시동 검증)
- [x] **첫 자동 평일 운영**: 다음 평일 09:00 직전 폰에서 시작 푸시 + `/status` 응답 정상이면 통과. `logs/paper-YYYY-MM-DD.log` 에 `runner.bar` 누적 확인. (4/29 자동 시동 → 매수 시뮬 2건 + `/pause` 응답 + 5/1 시작 푸시 9:07 도착으로 일자 간 자동 재시동도 확인.)
- [x] **회사에서 v3 명령 검증**: 폰에서 `/pause` → 응답 + 다음 신호에서 `runner.order_skipped_paused` 로그. `/resume` 으로 복귀 후 `/stop` 으로 graceful 종료. (4/29 `/pause` 응답 200 OK 통과. `/resume`·`/stop` 은 미경유였으나 5/1 launchd auto-stop 시 "⏹ paper 정지(정상 종료)" 푸시 16:00 도착 = SIGTERM → graceful shutdown 핵심 경로 검증 완료. PR #14 + #15 가 잡은 두 단계 hang 버그 회귀 게이트 통과.)

---

## 📘 19.5단계 · 초보자용 매뉴얼 — **✅ 완료** (2026-05-03)

단계라기보단 부수 작업이지만 두 번에 걸쳐 머지된 매뉴얼 흔적을 한눈에 보이게 묶음.

- [x] **PR #20 · `manual.md` 신설** · 한국어 초보자 가이드. 0단계 사전 준비부터 운영·트러블슈팅까지 한 파일.
- [x] **PR #28 · §11 day-trade 모드 섹션** · 21단계 paper-day 사용법·로그·텔레그램·트러블슈팅까지 추가 (21단계 마무리 시점에 갱신).

---

## 🧹 20단계 · 코드 리팩토링 (구조만, 동작 보존) — **✅ 완료** (2026-05-02)

19단계까지 누적된 기능들로 두 큰 파일이 비대해져 가독성이 떨어졌다. 동작·테스트·로그 키 모두 보존한 채 책임만 쪼갬.

- [x] **PR #17 · `runner._on_bar` 75줄 → 12줄 분할** · `_evaluate_signal`(리스크 평가 + PAUSED/dry-run 게이트, Order 또는 None 반환) + `_place_and_track`(REST 송신 + 성공·실패 분기 + 로컬 포지션 갱신 + notifier) 두 helper 로 분리. `_on_bar` 는 흐름만 보이는 오케스트레이터로. 외부 시그니처·로그 키 6종·notifier 키 2종·게이트 우선순위(REJECT → PAUSED → dry-run → 실주문) 모두 동일.
- [x] **PR #18 · `cli._fetch_daily_bars` 헬퍼 추출** · backtest / strategy advise / strategy suggest 가 각자 6줄로 반복하던 `KiwoomRestClient → get_daily_bars` + 빈 결과 가드를 한 곳에. 빈-결과 메시지가 세 곳에서 미묘하게 다르던 것 통일. df 변환·`--from` 필터는 호출자 책임으로 남김 (명령마다 처리 방식이 달라서).
- [x] **품질 게이트** · 단위 155건 / 통합 4건 두 PR 모두 무수정 통과 / ruff·format·mypy 클린.

---

## 📈 21단계 · Day-Trading 모드 전환 — **✅ 완료** (2026-05-03)

기존 swing 방식(`--symbols` 로 종목 고정)에서 → **매일 아침 거래량 상위 종목을 골라 그날 안에 청산**하는 day-trade 방식으로 확장. 사용자 결정: KOSPI+KOSDAQ 통합 거래량 상위 3종목, 시장 5/20일선 게이트, 발굴 직후 즉시 균등 매수, 15:15 KST 일괄 시장가 청산. 기존 `paper --symbols / --profile` 은 보존하고 신규 `paper-day` 명령으로 분기. 3개 PR 로 쪼개서 진행.

### Phase A · 키움 어댑터 (거래량 순위 + 시장 지수)

- [x] **`endpoints.py`** · `TR.RANK_VOLUME` (거래량 상위, 후보 `ka10030`) / `TR.SECTOR_INDEX_DAILY` (업종 지수 일봉, 후보 `ka20006`) + `PATH_RANK` / `PATH_SECTOR` 추가. 정확한 코드는 probe 로 확정.
- [x] **`market/rank.py`** · `top_volume(client, top_n=3, markets=("KOSPI","KOSDAQ"))` → `list[RankedSymbol{symbol, market, name, volume, price}]`. KOSPI/KOSDAQ 각각 호출 → 거래량 desc 정렬 → top_n. 한쪽 시장 실패해도 다른 쪽으로 진행.
- [x] **`market/index.py`** · `index_gate(client, market)` → `IndexGate{market, last, sma5, sma20, above_5_and_20}`. 일봉 30봉 → 5/20 SMA. 데이터 부족·API 실패 시 보수적으로 `above=False`.
- [x] **`scripts/probe_rank_index.py`** · `probe_orders.py` 패턴. 모의 서버 응답 dict 캡처 → endpoints.py 확정용.
- [x] **테스트 단위** — `test_market_rank.py` (정렬·top_n·시장 합성·실패 격리·prefix 정규화 4건) + `test_market_index.py` (SMA above/below·데이터 부족·KOSDAQ 코드·API 실패 보수 5건).

### Phase B · 청산 스케줄러 + 동적 종목 슬롯

- [x] **`runner.py`** · `liquidate_at: time | None`, `liquidate_grace_sec` 필드. `set_universe(symbols, strategies)` 동적 주입(ws.start 이전에만). `liquidate_all(rest, account, reason)` 보유 전부 시장가 SELL — dry_run 시뮬·1회 가드·일부 실패 격리. 백그라운드 task `_liquidation_loop` 가 KST 시계 1초 폴링 → 도달 시 청산 + `request_stop()`. `liquidate_at=None` 이면 기존 swing 동작 100% 유지. `_universe_ready` 이벤트로 universe 대기 중 stop 안전.
- [x] **`strategy/intraday.py`** · `IntradayMomentum` @register("intraday_momentum") — 첫 봉에 BUY `weight` 한 번만 발사 후 NONE. orchestrator 가 `weight=1/N` 주입.
- [x] **테스트** — `test_runner_liquidate.py` (dry_run/실주문/일부실패/idempotent/시계 트리거/STOPPING 조기/snapshot 없음 + 전략 발사 9건) + `test_runner_dynamic_universe.py` (swing 회귀/day-trade 빈 universe/검증 4종/ws 활성 후 차단/request_stop·set_universe 깨움/swing 즉시 ready 9건).

### Phase C · `paper-day` CLI + launchd 통합

- [x] **`cli.py paper_day(top_n, liquidate, dry_run, allow_real, minutes, discover_delay)`** · 빈 universe + `liquidate_at=15:15` 로 러너 생성 → 백그라운드 시동 → `_delayed_discover` 가 `discover_delay_sec` 후 `discover_and_arm` → 청산까지 await. 발굴 실패 시 `request_stop` 으로 hang 방지.
- [x] **`daytrade/orchestrator.py`** · `discover_and_arm(rest, runner, top_n, strategy_factory, markets)` → 발굴+게이트(시장당 1회 캐싱)+set_universe 트랜잭션. `DiscoveryResult{chosen, blocked, gates, note_ko}`.
- [x] **`scripts/paper-day-mode.sh`** + **`launchd/com.mizeloble.auto-trade.paper-day.plist`** · 평일 08:55 시동, 별도 stop plist 불필요(러너 자체 청산 후 종료). 기존 swing plist 와 동시 활성 X — `launchd/README.md` 에 전환 절차 안내.
- [x] **`notify/state.py`** · `RunnerStateSnapshot.runner_mode` (`swing`/`day-trade`) + `liquidate_at_label` 필드. `format_status` 가 day-trade 모드면 "▶ 운영중 · day-trade (청산 15:15)" 처럼 표시.

### 사이드 픽스 — probe 실측으로 endpoints/필터 3차 보정 (2026-05-03)

PR #21~#23 머지 직후 사용자가 `scripts/probe_rank_index.py` 를 모의서버에 돌리며 발견한 거절을 3차에 걸쳐 잡음. main 회귀 게이트로 단위 테스트 7건 추가.

- [x] **PR #24 · probe 1차** — `ka10030` 필수 필드 9개 (`crd_tp` / `trde_qty_tp` / `pric_tp` / `trde_prica_tp` / `mrkt_open_tp` 추가). `TR.SECTOR_INDEX_DAILY` `ka20006`(현재가) → **`ka20009`(업종일봉)** 교체. 단위 196 → 197.
- [x] **PR #25 · probe 2차** — 통합거래소 종목 코드 `_AL`/`_NX` suffix 처리 (`lstrip("A") → split("_")[0]`). `ka20009` 의 `mrkt_tp` 필수 (KOSPI=`"0"` / KOSDAQ=`"1"`, 순위 TR 의 `MARKET_TP_*` 와 분리). 단위 197 → 199.
- [x] **PR #26 · probe 3차** — `ka20009` 응답 행 키가 `cur_prc_n` (suffix `_n`) — `_parse_close` 후보 체인에 추가하지 않으면 모든 일봉 None → 게이트 무조건 False 라는 silent failure 였음. + KOSPI 거래량 1~5위가 KODEX 인버스/레버리지·KOSDAQ 1위가 SPAC 인 실측 결과로 **`exclude_etf=True` 기본** + `--include-etf` 플래그. 단위 199 → 203.

### 사이드 픽스 — 사용자 dry-run 검증으로 발견된 hang/UX 보정 (2026-05-03)

5/3 21:42 사용자 dry-run 실측에서 발견한 두 결함 + 후속 사용자 피드백 1건. 단위 210건까지.

- [x] **PR #27 · universe 0건 hang fix + 텔레그램 한국어 포맷** — 게이트 차단으로 `chosen=[]` 가 나오면 `set_universe()` 호출 안 함 → `_universe_ready` 안 set → `run()` 이 universe 대기에서 무한 hang(청산 시계 task 는 `ws.start` 이후라 안 도착). `_delayed_discover` 끝에 `if not result.chosen: runner.request_stop()` 추가. 동시에 `daytrade.discovery_done`/`runner.liquidate_done`/`runner.liquidate_failed` 3종 한국어 템플릿 추가(기존엔 raw kwargs 로 노출됐음). 회귀 게이트 5건. 단위 203 → 208.
- [x] **PR #29 · stopped 후 텔레그램 명령 비활성 안내** — PR #27 의 자기 종료 흐름이 의도된 동작이지만 사용자가 `/status` 보내고 무응답에 헷갈림. `runner.stopped` 메시지의 reason 이 `stopped_before_universe` / `no_candidates_stopping` 일 때 한 줄 안내 추가. 일반 stop(SIGTERM·`/stop`) 은 안내 X. 단위 208 → 210.

### 품질 게이트

단위 210건(이전 155 + 21단계 신규 55) PASS, 통합 4건 그대로, ruff·format·mypy 클린.

### 체험 확인 (코드 수준)

- [x] **PR-A**: `uv run pytest tests/test_market_rank.py tests/test_market_index.py` → 9건 PASS.
- [x] **PR-B**: `uv run pytest tests/test_runner_liquidate.py tests/test_runner_dynamic_universe.py` → 18건 PASS (사용자 검증 통과 2026-05-03).
- [x] **PR-C**: `uv run pytest tests/test_daytrade_orchestrator.py tests/test_cli_paper_day.py` → 14건 PASS.

### 체험 확인 (사용자가 직접 — 머지 후)

- [x] **probe 실측** · 5/3 3차에 걸쳐 진행 → PR #24/#25/#26 으로 endpoints·필드·ETF 필터 모두 보정. 4건 호출 모두 `return_code=0` + 응답 본체 매핑까지 확정.
- [x] **dry-run 종일** · 5/3 21:42 1차(약세장 `chosen=[]` 자기 종료) + 5/6 강세장 풀 사이클(KOSPI 게이트 통과 → 005930·001500·006800 균등 매수 시뮬 → 15:15 청산 → 일일 리포트 푸시) 검증됨.
- [x] **launchd 전환** · day-trade plist 등록 + `kickstart -k` 즉시 시동 검증 (2026-05-05 21:53 KST). 발굴(018880·010170·001510, ETF 53건 제외) → 시장 게이트 통과 → `ws.subscribe`/`ws.connected` → 청산시각 경과로 graceful stop 까지 풀 흐름. 텔레그램 3종 푸시(🔍 발굴 완료 / ▶️ paper 시작 / ⏹ paper 정지) 도착 확인.
- [x] **mock 실주문 전환** (2026-05-06 사용자 승인) · `paper-day-mode.sh` 의 `--dry-run` 을 `--no-dry-run` 으로 변경. KIWOOM_MODE=mock 유지(진짜 돈 X) 하면서 키움 모의투자 서버에 실제 주문 송신 → 체결/잔고/청산 라이프사이클 검증 단계. real 전환은 별도 명시 승인 필요.
- [ ] **mock 실주문 검증 관찰** · 다음 평일 실행 후 `logs/paper-day-YYYY-MM-DD.log` 에 `runner.order_accepted`(`order_simulated` 가 안 보여야 OK) + `runner.liquidate_accepted` + 키움 mock 잔고에 실제 보유/realized PnL 반영 확인. 며칠 안정적이면 real 전환 후보.

---

## 📊 22단계 · 일일 리포트 + 주간 인사이트 — **✅ 구현 완료** (2026-05-05)

매일 장 끝나고 그날 결과 자동 푸시(템플릿) + 주 1회 누적 패턴 LLM 분석. 사용자 합의: 매일 LLM 호출은 비용·메모리 규칙 둘 다 비효율 → 자동은 템플릿, LLM 은 명시 트리거 또는 주 1회 저빈도 cron.

### Phase A · 자동 일일 리포트 (템플릿, LLM 무관) — 완료

- [x] **`reports/log_parser.py` + `reports/daily.py`** · structlog JSON 라인 → `LogEvents` → `DailyReport` (발굴 종목·매수 시뮬·청산·시장 게이트·에러 카운트). 결정론적·외부 호출 X.
- [x] **포맷터** · `notify/state.format_daily_report` — 한국어 이모지 메시지. `runner.daily_report` 이벤트 키.
- [x] **러너 hook** · `notify/reporting.ReportingNotifier` wrapper — `runner.stopped` 인터셉트해 daily_report 1회 발사. 트레이딩 핫패스 영향 X, 실패 swallow.
- [x] **CLI** · `auto-trade report --date YYYY-MM-DD [--push]` 수동 재생성. paper-day 가 자동으로 ReportingNotifier 로 wrapping.
- [x] **데이터 영속화** · `data/reports/daily-YYYY-MM-DD.json` 라운드트립 + Phase B 입력으로 그대로 사용.
- [x] **테스트 29건 신규** — log_parser(5) + daily(8) + reporting(6) + state daily_report(5) + cli report(5).

### Phase B · 주간 인사이트 (LLM, on-demand + 토요일 1회) — 완료

- [x] **`notify/insight.py`** · `strategy/llm_advisor.py` 구조 재활용. system 프롬프트 `cache_control: ephemeral` + `propose_insight` tool_use 강제. 풀 JSON 대신 요약 dict (chosen_freq / pnl 통계 / 게이트 통과율) 만 LLM 에 전달.
- [x] **CLI** · `auto-trade insight --days 7 [--dry-run] [--push]` — `--dry-run` 은 키 없이 입력 요약만 출력.
- [x] **텔레그램 `/insight` 명령** · `TelegramCommandHandler` 에 `on_insight` 콜백 + 5분 쿨다운 + 비활성 안내. `paper-day` 가 ANTHROPIC_API_KEY 있을 때만 콜백 주입.
- [x] **launchd weekly job** · `scripts/launchd/com.mizeloble.auto-trade.insight-weekly.plist` (매주 토 09:00 KST) + `scripts/insight-weekly.sh`. README 에 등록·해제·수동 호출 안내.
- [x] **메모리 규칙 갱신** · `feedback_llm_cost.md` 에 "주 1회 이하 저빈도 cron 허용" 명시 + MEMORY.md 인덱스 동기화.
- [x] **비용 가드** · 호출당 약 $0.05~0.10 추정. `--dry-run` 으로 키 없이 입력 검증, system 프롬프트 캐싱으로 토큰 절약, `Settings.anthropic_model` 로 Sonnet/Haiku 다운그레이드 가능.
- [x] **테스트 26건 신규** — insight(12: collect/summarize/insight tool_use/format) + telegram /insight(8) + cli insight(6).

### 품질 게이트

단위 239 → **265건** (+26 Phase B + 29 Phase A 합치면 +55). ruff/format/mypy(src) 클린.

### 체험 확인 (사용자가 — 다음 운영 사이클에서)

- [ ] **Phase A**: 다음 평일 매수 들어가는 날 청산 직후 텔레그램에 "📊 일일 리포트" 도착. 종목별 손익 % + 시장 게이트 + 에러 카운트 정확.
- [ ] **Phase B 수동**: `.env` 에 `ANTHROPIC_API_KEY` 채운 뒤 `uv run auto-trade insight --days 1 --dry-run` → 입력 요약 출력. 며칠 운영 후 `--no-dry-run` 으로 LLM 호출 검증.
- [ ] **Phase B 텔레그램 `/insight`**: paper-day 운영 중 폰에서 `/insight` → "🧭 주간 인사이트" 도착 (1회) + 즉시 재호출 시 "쿨다운" 안내.
- [ ] **Phase B 자동**: `launchctl bootstrap gui/$UID scripts/launchd/com.mizeloble.auto-trade.insight-weekly.plist` 등록 후 첫 토 09:00 자동 푸시 도착.

---

## 🎯 23단계 · paper-day 정통 데이트레이딩 전환 (C 옵션) — **진행 중** (2026-05-06 ~)

현재 paper-day 는 09:00 첫 봉 1회 매수 → 15:15 일괄 청산의 단발 형태. 정통 데이트레이딩에 필요한 위험 통제(stop·트레일링·일일 강제청산), 수익 보존(부분 익절), 비용 가시화, 다회 진입, 지수 일중 모멘텀 트리거를 8개 Phase 로 나눠 추가. 각 Phase = 1 PR. Plan: `~/.claude/plans/c-witty-hartmanis.md`.

### Phase A · 비용 모델 갱신 + 일일 리포트 비용 차감 PnL — **✅ 완료** (2026-05-06)

- [x] **`backtest/broker.py` + `engine.py`** · `tax_rate_sell` 0.0018 → **0.0020** (2026 거래세 인상). `slippage_bps` 0 → **5.0** (0.05% 보수적). docstring·CLAUDE.md 표기 갱신.
- [x] **`reports/daily.py`** · `DailyReport` 에 `total_fees`/`total_taxes`/`total_slippage`/`realized_pnl_after_cost` 4개 필드 + `_estimate_costs` 함수. broker 의 비용 상수와 일치하는 단일 진실 출처 상수(`COST_*`) 별도 정의 — reports/ 가 backtest/ 를 import 하지 않도록(외부 호출 금지 규칙).
- [x] **`notify/state.format_daily_report`** · realized_pnl 과 차감치가 다를 때만 "💸 비용 차감 PnL: ±X,XXX원" 별도 라인.
- [x] **테스트** · `tests/test_reports_daily.py` 비용 4건 추가 (정상 추정·완전 라운드트립·매매0/가격None 시 None) + `tests/test_notify_state_daily_report.py` 비용 라인 표시·생략 2건 추가 + `tests/test_backtest_engine.py` 슬리피지 0 명시 주입 회귀 1건 갱신. 271/271 PASS, ruff/mypy 클린.
- [ ] **검증** (사용자): `uv run auto-trade report --date <오늘> --push` 또는 다음 paper-day 자동 푸시에서 "💸 비용 차감 PnL" 라인 도착 확인.

### Phase B · 1회/일일 절대금액 한도 + kill switch — **✅ 완료** (2026-05-06)

- [x] **`risk/manager.py RiskLimits`** · `max_order_value` (1회 주문 원화 절대 한도, 0=무제한), `daily_notional_limit` (일일 총 매수대금, 0=무제한), `daily_loss_kill` (True 면 강제 청산) 3개 추가. 기본값 모두 보수 측 비활성.
- [x] **`AccountState.today_notional_used`** · 러너가 dry-run 시뮬·실거래 BUY 체결 후 누적. 잔고 갱신 시 carry-over (자정 리셋 = 러너 재시작).
- [x] **`evaluate()` BUY 분기** · 비중·노출·예수금 기반 사이징 결과를 max_order_value 로 capping → daily_notional_limit 도달 여부 / 남은 한도로 추가 capping.
- [x] **`should_force_liquidate(account) -> bool`** · 새 메서드. `daily_loss_kill=True` + 손실 한도 도달 시 True. `runner.run()` 의 잔고 갱신 직후 체크해 True 면 `liquidate_all + request_stop` + `runner.kill_switch_triggered` 텔레그램 푸시.
- [x] **`notify/telegram.py`** · `runner.kill_switch_triggered` 한국어 포맷 ("🛑 일일 손실 한도 도달 (-3.0%) — 강제 청산 후 정지").
- [x] **테스트** · `tests/test_risk_manager.py` 7건 (capping·rejection·kill switch on/off/threshold) + `tests/test_notify_telegram.py` 1건. 293/293 PASS, ruff/mypy 클린.
- [ ] **검증** (사용자): YAML 프로파일 또는 CLI 에서 한도 활성 후 mock dry-run 에서 `risk.reject` / `runner.kill_switch_triggered` 로그·푸시 확인 (Phase G 의 --profile 도입 후 본격 검증).

### Phase C · 종목별 stop-loss + 트레일링 + 부분 익절 (PositionGuard) — **✅ 완료** (2026-05-06)

- [x] **`risk/position_guard.py`** · 신규 모듈. `PositionGuard` 가 종목별 `_Tracking(avg_price, highest_seen, partial_taken)` 추적. 우선순위: stop_loss → trailing → partial_take. `is_active` 로 모두 0 인 비활성 상태 빠른 단락. `reset(symbol)` 로 다회 진입 사이의 상태 초기화.
- [x] **`RiskLimits`** · `stop_loss_pct`/`trailing_stop_pct`/`partial_take_pct`/`partial_take_qty_ratio` 4개 필드 추가, 기본 0(비활성)·0.5(절반).
- [x] **러너 통합** · `_on_bar` 끝에 `_run_position_guard` → 발화 시 `_place_guard_action` 으로 즉시 SELL 송신. 전략 시그널 채널과 별개. dry-run·PAUSED·실거래 분기 모두 처리. 구조화 로그 `position_guard.{stop_loss|trailing|partial_take|fired|skipped_paused}`.
- [x] **텔레그램 알림** · `position_guard.fired` 이벤트 한국어 포맷 (🛟 손절 / 📉 트레일링 / 💵 부분 익절).
- [x] **테스트** · `tests/test_position_guard.py` 11건 (stop 발동·트레일 발동·partial 후 트레일·우선순위·quantity 0·reset·전량 ratio·비활성) + `tests/test_notify_telegram.py` 2건. 306/306 PASS, ruff/mypy 클린.
- [ ] **검증** (사용자): YAML 프로파일에서 한도 활성 후 mock dry-run 에서 `position_guard.*` 로그 + 텔레그램 발화 확인 (Phase G 의 --profile 도입 후).

### Phase D · 다회 진입 + 쿨다운 (IndexMomentumDayTrade 신규) — **✅ 완료** (2026-05-06)

- [x] **`strategy/index_daytrade.py`** · 신규 `IndexMomentumDayTrade` (`@register("index_momentum_daytrade")`). state 의 `last_entry_ts`/`last_exit_ts` 비교로 보유 추정 + cooldown_min 경과 시 재진입.
- [x] **러너 `_mark_exit(symbol, ts)`** · `_place_and_track` SELL 잔량 0 분기 + `_place_guard_action` 끝에서 호출. strategy.state["last_exit_ts"] 갱신 + `position_guard.reset(symbol)` (재진입 시 highest_seen·partial_taken 초기화).
- [x] **registry 등록** · `load_builtin_strategies()` 에 `index_daytrade` import 추가. 기존 `intraday`(`IntradayMomentum`) 그대로 유지.
- [x] **테스트** · `tests/test_strategy_index_daytrade.py` 8건 — 첫 진입·보유 중 차단·쿨다운 차단·쿨다운 경과 재진입·zero 쿨다운·다회 사이클·reset·registry 조회. 314/314 PASS.
- [ ] **검증** (사용자): Phase G `--profile` 머지 후 YAML 의 `strategy: index_momentum_daytrade` + `params.cooldown_min` 설정으로 mock dry-run 에서 다회 진입 시퀀스 확인.

### Phase E · 지수(KOSPI/KOSDAQ) 일중 모멘텀 시세 — **✅ 완료** (2026-05-07)

- [x] **`market/index.py`** · `IndexMomentum` dataclass + `IndexMomentumTracker` 인메모리 시계열(보관 30분, 매칭 톨러런스 90초). 시장별 격리. push 가 cutoff 자동 정리.
- [x] **데이터 소스** · 별도 분봉 TR 없이 기존 `ka20009` 의 가장 최근 일봉 row close 를 "현재 지수값"으로 활용 — 오늘 일봉은 진행 중이라 실시간 갱신, probe 2026-05-03 의 `cur_prc_n` 필드 그대로.
- [x] **`fetch_latest_index_price` + `update_index_momentum`** · 1분 주기로 호출자(Phase F orchestrator)가 fetch+push+momentum 한 번에. 실패 시 None.
- [x] **테스트** · `tests/test_market_index_momentum.py` 12건 — 빈 history / lookback 매칭·tolerance / cutoff 정리 / 시장 격리 / fake client fetch · update 통합. 326/326 PASS, ruff/mypy 클린.
- [ ] **검증** (사용자): Phase F 머지 후 mock dry-run 에서 `market.index.momentum` 로그 (last + return_pct) 1분 단위로 찍히는지 확인.

### Phase F · 지수 모멘텀 게이트를 IndexMomentumDayTrade 에 결합 — **✅ 완료** (2026-05-07)

- [x] **전략 파라미터** · `momentum_lookback_min`/`momentum_threshold_pct`/`momentum_tracker`/`market` 추가. 진입 조건에 "지수 모멘텀 ≥ threshold" AND. threshold=0 이면 게이트 비활성(기본).
- [x] **주입 방식** · `Bar.meta` 추가 대신 전략 init 시 `IndexMomentumTracker` 직접 주입 — 전략은 인메모리 조회만(부작용 X), strategy/CLAUDE.md "외부 부작용 금지" 규칙 유지.
- [x] **`daytrade/orchestrator.py`** · `start_momentum_loop(rest, tracker, markets, interval_sec, lookback_min)` helper. `_momentum_loop` 가 `update_index_momentum` 을 주기 호출하며 실패는 swallow + 다음 사이클 재시도.
- [x] **테스트** · 전략 게이트 5건 (차단/통과/threshold=0 비활성/tracker 누락 시 차단/history 부족) + orchestrator loop 2건 (cancel 시 정리·실패 swallow). 333/333 PASS, ruff/mypy 클린.
- [ ] **검증** (사용자): Phase G 머지 후 YAML 의 `params.momentum_threshold_pct: 0.3` 으로 mock dry-run 에서 게이트 통과/차단 로그 확인.

### Phase G · paper-day --profile + day-trade YAML 프로파일 — **✅ 완료** (2026-05-07)

- [x] **`daytrade/profile.py`** 신규 · `DaytradeProfile` (pydantic) — name/strategy/top_n/liquidate/exclude_etf/discover_delay_sec + `params` (cooldown/momentum) + `risk` (stop_loss/trailing/partial_take/max_order/daily_*). swing 의 `StrategyProfile` 와 별개 — day-trade 는 종목 미지정 + 한도가 풍부해 별도 모델이 더 깔끔. 검증: top_n ≤ 10, stop_loss ≤ 10%, liquidate HH:MM 형식.
- [x] **`strategies/profiles/daytrade-default.yaml`** · 보수적 비활성 기본 (모든 한도 0/false) + 한국어 주석 안내.
- [x] **`daytrade/cli_helpers.py`** · `apply_profile_to_runner` (RiskLimits 인플레이스 반영) + `build_profile_factory` (전략 팩토리) + `_spawn_momentum_task` (게이트 활성 시 별도 KiwoomRestClient 컨텍스트로 1분 모멘텀 loop). paper_day_main/with_commands 가 `profile`/`momentum_tracker` 인자 받음.
- [x] **`orchestrator.discover_and_arm`** · `strategy_factory` 시그니처 `(symbol, *, market)` 으로 확장 — IndexMomentumDayTrade 가 종목별 시장 정보를 받도록.
- [x] **`cli.py paper_day`** · `--profile <path>` 옵션. 지정 시 top_n/liquidate/discover_delay/exclude_etf/RiskLimits 모두 프로파일 값으로 덮어쓰고 IndexMomentumTracker 자동 생성.
- [x] **테스트** · `tests/test_daytrade_profile.py` 8건 (라운드트립·검증·샘플 yaml 로드) + `tests/test_cli_paper_day.py` 1건 (--profile override). `tests/test_daytrade_orchestrator.py` 기존 fake factory 시그니처 갱신. 342/342 PASS, ruff/mypy 클린.
- [ ] **검증** (사용자): `uv run auto-trade paper-day --profile strategies/profiles/daytrade-default.yaml --dry-run` mock 에서 정상 시동 확인. 한도 1~2개(예: stop_loss_pct=0.01) 켜고 며칠 dry-run 후 본격 활성.

### Phase H · 텔레그램 5분 요약 throttle — **✅ 완료** (2026-05-07)

- [x] **`notify/throttle.py`** 신규 · `ThrottledNotifier` 가 다른 Notifier 를 wrap. 매매 이벤트(order_accepted/failed, liquidate_done/failed, position_guard.fired, daytrade.discovery_done) 만 buffer 에 한 줄씩 모았다가 N분 윈도우 경과 시 또는 aclose() 시 inner 에 `notify.throttled_summary` 1회 푸시. 즉시 통과: runner.started/stopped/kill_switch/balance_failed/weekly_insight/daily_report.
- [x] **윈도우 트리거** · timer task 없이 매 notify 시점에 `_maybe_flush` 가 동작 → 단순·시뮬·테스트 용이. 알 수 없는 이벤트는 통과(unknown event passthrough).
- [x] **`cli.py paper-day --telegram-throttle <int>`** 옵션 (0=비활성, 5 권장). 활성 시 inner_notifier 를 ThrottledNotifier 로 wrap. ReportingNotifier(일일 리포트) 와 호환 — daily_report 는 passthrough 라 자동 통과.
- [x] **`notify/telegram.py`** · `notify.throttled_summary` 이벤트 케이스 추가 (text 본문 그대로).
- [x] **테스트** · `tests/test_notify_throttle.py` 7건 (passthrough/buffer/aclose flush/empty/unknown event/window reset/partial_take 라벨). 349/349 PASS, ruff/mypy 클린.
- [ ] **검증** (사용자): `paper-day --telegram-throttle 5` mock dry-run 에서 다회 매매 시 5분마다 "🔁 최근 5분 요약" 1개 푸시 확인. 즉시 푸시 이벤트(kill switch 등) 는 윈도우와 별개.

### 23단계 검증 (전 Phase 완료 시)

- [ ] `uv run pytest` 전부 PASS (신규 LOC ~600 + 테스트 ~500 예상).
- [ ] mock `uv run auto-trade paper-day --profile strategies/profiles/daytrade-default.yaml --dry-run` 에서 `daytrade.universe_chosen` → `index.momentum` → `runner.order_simulated`(×N) → `position_guard.{stop|trailing|partial}` 1개 이상 → `runner.liquidate_simulated` → `runner.stopped` 시퀀스.
- [ ] daily report JSON 에 `realized_pnl_after_cost` 정상.
- [ ] kill switch 시뮬에서 강제 청산 + 종료 흐름.

---

## 🗞 24단계 · 주간 뉴스 → 다음 주 day-trade 전략 자동 반영

토요일 09:00 KST 인사이트 cron 에 뉴스 수집·LLM 분석·YAML 프로파일 자동 갱신 단계 추가. LLM 호출은 주 1회 한정 ([feedback_llm_cost](memory)). 23단계 G(--profile) 가 머지된 뒤 진행.

### Phase A · 한국 시장 RSS 뉴스 수집기 — **✅ 완료** (2026-05-07)

- [x] **`news/collector.py`** 신규 + feedparser 의존 추가 (`uv add feedparser`). RSS feed → `NewsItem(title, summary, url, published_at, source, tickers)` 정규화.
- [x] **기본 RSS** · 한국경제(`hankyung.com/feed/all-news`) + 매일경제(`mk.co.kr/rss/30000001`) 공개 feed. 네이버 금융은 공식 RSS 미제공이라 대체. 사용자가 `feed_urls` 인자로 override 가능. User-Agent 명시 (`auto-trade-bot/1.0`).
- [x] **`collect_recent_news(days, feed_urls, cache_root, now, fetcher)`** · 같은 날 재호출 시 `data/news/news-YYYY-MM-DD.json` 캐시 히트 → fetcher 안 부름. fetch/parse 실패는 swallow + 다음 feed 시도 (kiwoom 5xx 같은 transient 보호). 결과는 published_at 내림차순.
- [x] **종목 코드 추출** · title+summary 에서 6자리 정규식 → 정렬·중복제거 tuple. 이름→코드 매핑은 LLM 본문 판단으로 위임.
- [x] **테스트** · `tests/test_news_collector.py` 10건 (parse_feed 5 + collect 5: 캐시 hit·days 필터·fetch 실패·라운드트립·정렬). 359/359 PASS, ruff/mypy 클린.
- [ ] **검증** (사용자): `python -c "from auto_trading.news import collect_recent_news; print(len(collect_recent_news(days=1)))"` 로 실 RSS 호출 확인.

### Phase B · 주간 LLM 입력에 뉴스 + 다음주 프로파일 제안 — **✅ 완료** (2026-05-07)

- [x] **`news/proposal.py`** 신규 — `notify/insight.py` 와 별개 모듈. 인사이트는 "지난 주 결과 사람용 요약", proposal 은 "다음 주 day-trade 프로파일 어떻게 바꿀지 → Phase C YAML 자동 갱신용".
- [x] **`DaytradeProposal`** — pydantic. summary_ko/rationale_ko/confidence 필수 + 변경 옵션(top_n, cooldown_min, momentum_*, stop_loss_pct, trailing_stop_pct, partial_take_*, daily_loss_kill) 모두 None 가능 → "유지". themes_to_watch/symbols_to_avoid 옵션 리스트.
- [x] **`propose_daytrade_profile(reports, news, ..., client, current_profile, model)`** · 강제 도구 `propose_daytrade_profile` tool_use + Pydantic 검증. system 프롬프트 cache_control: ephemeral (재호출 시 토큰 절약).
- [x] **`_summarize_reports/_news/_current_profile`** · 토큰 예산: 뉴스 30건 + 본문 240자 truncate (collector 에서) → input ~2000 tokens / output ~1024. 호출당 추정 $0.04~0.08 (Opus 가격 기준, 주 1회 = 월 $0.16~0.32).
- [x] **`format_proposal_message`** · 텔레그램·CLI 출력. 변경 파라미터 / 주목 테마 / 회피 종목 / 근거 / 신뢰도 섹션.
- [x] **`build_proposal_callback`** · 비동기 콜백 (Phase D 토요일 cron 에서 호출 예정). reports/news 모두 비면 None 반환.
- [x] **테스트** · `tests/test_news_proposal.py` 8건 (minimal/with_changes/invalid validation/no tool_use/payload 전달/caching·forced tool/format 변경 + 노 변경). 367/367 PASS, ruff/mypy 클린.

### Phase C · daytrade-current.yaml 자동 갱신 + 백업 + 알림 — **✅ 완료** (2026-05-07)

- [x] **`news/profile_writer.py`** 신규 — `apply_proposal` + `write_with_backup` + `apply_and_persist` (1차 진입점) + `format_apply_message`.
- [x] **`SafetyCaps`** · top_n ≤ 5, stop_loss/trailing ≤ 3%, partial_take ≤ 5%, momentum_threshold ≤ 3%, cooldown ≤ 120, lookback ≤ 30. `block_daily_loss_kill_change` 옵션으로 daily_loss_kill 자동 변경 차단도 선택.
- [x] **백업** · 기존 yaml 이 있으면 `<name>.bak.YYYY-MM-DD` 로 shutil.copy2. 첫 작성 시 백업 X. 같은 날 두 번이면 덮어쓰기 (cron 1회 가정).
- [x] **변경 diff** · `ApplyResult.accepted`/`rejected` 한국어 라인. `format_apply_message` 가 변경/거부/테마/회피/근거/신뢰도/백업명 텔레그램 메시지 조립.
- [x] **`apply_and_persist`** · current 없으면 fallback(`daytrade-default.yaml`) → 둘 다 없으면 기본 인스턴스. last_updated_at 자동 갱신.
- [x] **테스트** · `tests/test_profile_writer.py` 15건 (no change/change/cap reject/부분 거부+부분 통과/daily_loss_kill 차단/동일값 skip/backup 생성·생략/풀 사이클/fallback/format 3종/메타 보존). 382/382 PASS, ruff/mypy 클린.

### Phase D · 토요일 cron 통합 + paper-day 자동 사용 — **✅ 완료** (2026-05-07)

- [x] **`auto-trade weekly-proposal`** CLI 신규 — `--days-reports`/`--days-news`/`--current`/`--fallback`/`--target`/`--push`/`--dry-run`. dry-run 은 LLM 호출 없이 입력 요약 출력. 정상 호출 시 collect_recent_news + collect_recent_reports → propose_daytrade_profile (LLM) → apply_and_persist (안전 캡 통과한 변경만 yaml 작성 + 백업) → format_apply_message → 콘솔 + 옵션 텔레그램.
- [x] **`scripts/insight-weekly.sh`** · `auto-trade insight --push` 다음에 `auto-trade weekly-proposal --push` 체이닝. 제안 단계 실패해도 인사이트는 이미 푸시됐으니 `|| true` 로 cron 자체는 성공 마감.
- [x] **`scripts/paper-day-mode.sh`** · `daytrade-current.yaml` 가 있으면 `--profile` 자동 추가. 없으면 `daytrade-default.yaml` 폴백. 둘 다 없으면 CLI 인자만으로 동작 (안전).
- [x] **`notify/telegram.py`** · `runner.weekly_proposal` 이벤트 처리 (text 본문 그대로).
- [x] **`manual.md` §11** · "23단계 정통 데이트레이딩 옵션 + YAML 프로파일" + "24단계 주간 뉴스 → 다음 주 day-trade 프로파일 자동 반영" 두 섹션 추가.
- [x] **테스트** · `tests/test_cli_weekly_proposal.py` 4건 (no data exit / dry-run / 풀 사이클 / news-only path). 386/386 PASS, ruff/mypy 클린.

### 24단계 검증 (전 Phase 완료 시)

- [ ] 토요일 09:00 KST 에 `auto-trade weekly-proposal --push` 실행 → 뉴스 수집 + LLM 1회 + `daytrade-current.yaml` 갱신 + 텔레그램 "🗞 다음 주 day-trade 프로파일 갱신" 도착.
- [ ] 한도 초과 응답 시 자동 거부 + 경고 푸시.
- [ ] 다음 월요일 08:55 시동 시 paper-day 가 `daytrade-current.yaml` 자동 사용 (콘솔에 "프로파일 로드:" 라인).

### 사이드 픽스 — RSS feed 시장 특화 + 캐시 schema 버전 (2026-05-07, PR #47)

24단계 Phase D 직후 `weekly-proposal --dry-run` 검증에서 발견 — 한경 all-news + 매경 30000001 이 사회·연예 뉴스 위주라 LLM 입력 노이즈가 많음.

- [x] **`DEFAULT_FEED_URLS` 교체** · 한국경제 finance + economy 두 카테고리 (각 50건). 매경 `file.mk.co.kr/news/rss/...` 는 실측 빈 응답이라 제외. 사용자가 `feed_urls` 인자로 백업 추가 가능.
- [x] **`CACHE_SCHEMA_VERSION = "v2"`** · 캐시 파일명 `news-v2-YYYY-MM-DD.json` → URL 변경 시 기존 캐시 자동 무효화. 향후 NewsItem 스키마 변경 시 bump.

---

## ✅ 23단계 + 24단계 풀 완료 (2026-05-07)

| 단계 | Phase | PR |
|---|---|---|
| 23 | A. 비용 모델 (거래세 0.20% / 슬리피지 5bp) + 비용 차감 PnL | #35 |
| 23 | B. 1회/일일 절대금액 한도 + kill switch | #36 |
| 23 | C. PositionGuard (stop/트레일링/부분 익절) | #37 |
| 23 | D. IndexMomentumDayTrade (다회 진입 + 쿨다운) | #38 |
| 23 | E. 지수 일중 모멘텀 트래커 | #39 |
| 23 | F. 모멘텀 게이트 결합 + 1분 갱신 loop | #40 |
| 23 | G. paper-day --profile + DaytradeProfile YAML | #41 |
| 23 | H. 텔레그램 N분 요약 throttle | #42 |
| 24 | A. 한국 시장 RSS 뉴스 수집기 | #43 |
| 24 | B. 주간 LLM DaytradeProposal 제안 | #44 |
| 24 | C. yaml 자동 갱신 + 안전 캡 + 백업 | #45 |
| 24 | D. 토요일 cron 통합 + paper-day 자동 프로파일 사용 | #46 |
| ↳ | RSS 시장 특화 + 캐시 schema 버전 | #47 |

**총 변경량**: 신규/변경 코드 ~2,500 LOC + 테스트 ~1,500 LOC + 문서/스크립트 ~400 LOC, 386 단위 테스트 PASS, ruff/mypy 클린.

---

## 📅 25단계 · 운영 노이즈 정리 (휴장일 + 로그 회전)

23·24단계 풀 머지 후 모의 운영 품질 다지기. 사용자 브레인스토밍 후보 10개 중 신뢰성 우선 2건 선정.

### Phase A · KRX 휴장일 자동 skip — **✅ 완료** (2026-05-09)

- [x] **`src/auto_trading/market/calendar.py`** 신규 · `KRX_HOLIDAYS[2025|2026]` 정적 테이블 + `is_trading_day` / `next_trading_day` / `today_kst`. 매년 12월 KRX 발표 직후 다음 해 항목 1줄 추가 (todo-list 의 12월 알림으로 흡수). 도메인 규칙 준수: market/ 읽기 전용, KST 기준.
- [x] **`cli.py`** · `_holiday_guard(force, command_label)` 공용 helper + `paper` / `paper-day` / `weekly-proposal` 진입부 호출 + 각 명령에 `--force` 옵션. weekly-proposal 은 토요일 직접 휴장이라 다른 규칙 — 다음 거래일까지 5일 이상이면 LLM skip, 일반 토요일은 통과.
- [x] **`notify/telegram.py`** · `runner.holiday_skip` 이벤트 한국어 포맷 ("📅 KRX 휴장일(YYYY-MM-DD) — {command} 시동 생략 · 다음 거래일 …").
- [x] **테스트** · `test_market_calendar.py` 26건 (parametrize 합산) + `test_cli_holiday_guard.py` 7건 + `test_notify_telegram.py` 1건 + `test_cli_paper_day.py` autouse 평일 fixture. 424/424 PASS, ruff/mypy 클린.
- [x] **manual.md §11.25단계** · 자동 skip 동작 설명 + `--force` 우회 안내.
- [ ] **검증** (사용자): 다음 휴장일(2026-05-25 부처님오신날) launchd 자동 시동 시 텔레그램에 "📅 KRX 휴장일(2026-05-25) — paper-day 시동 생략" 도착 확인. paper-day-mode.sh 코드 변경 없이 paper-day 자체가 즉시 종료 — `logs/paper-day-2026-05-25.log` 가 짧게 마감.

### Phase B · 로그 회전·압축 — **✅ 완료** (2026-05-09, PR #53)

- [x] **`scripts/rotate-logs.sh`** 신규 · 30일↑ `*.log` → gzip -9 (`.log.gz`), 90일↑ `.log.gz` 삭제, launchd-*.log 10MB↑ truncate (launchd 가 같은 fd 유지하므로 회전 X — 비우고 timestamp 1줄). `LOGS_ROOT` env 로 대상 폴더 override (테스트용).
- [x] **`scripts/launchd/com.mizeloble.auto-trade.log-rotate.plist`** 신규 · 매일 02:30 KST 트리거. paper-day(08:55)/insight-weekly(토 09:00) 와 충돌 X.
- [x] **`launchd-install.sh` / `launchd-uninstall.sh`** · 새 라벨 등록·해제 + `chmod +x` 보장 + 안내 라인 추가. 세 잡(paper-day + insight-weekly + log-rotate) 사이클로 확장.
- [x] **`launchd/README.md`** · 세 잡 표 갱신.
- [x] **테스트** · `tests/test_scripts_rotate_logs.py` 8건 — recent kept / old gzipped / very-old gz 삭제 / launchd-* gzip 면역 / launchd-* 10MB+ truncate / 작은 launchd-* 그대로 / 빈 LOGS_ROOT silent exit / mixed pass. macOS BSD `stat -f%z` + zsh `(N)` 글롭 의존이라 `pytest.mark.skipif(sys.platform != "darwin" or shutil.which("zsh") is None)` 로 CI 분기.
- [x] **manual.md §11 25단계 Phase B** · 동작·수동 검증·즉시 시동 안내.
- [ ] **검증** (사용자): `./scripts/launchd-install.sh` 재실행 → `launchctl list | grep mizeloble.auto-trade.log-rotate` 출력 확인. `launchctl kickstart -k gui/$UID/com.mizeloble.auto-trade.log-rotate` 즉시 시동 후 `logs/launchd-log-rotate.out.log` 생성 확인. 1주 운영 후 30일 이상 된 `.log` 가 있다면 `.log.gz` 출현.

### 25단계 검증 (전 Phase 완료 시)

- [x] `uv run pytest` 432 PASS (Phase A 기준 424 + Phase B 8). ruff/format/mypy 클린.
- [ ] 다음 휴장일(2026-05-25 부처님오신날): launchd 자동 시동 → 텔레그램 휴장 안내 + `logs/paper-day-2026-05-25.log` 짧게 마감.
- [ ] 02:30 cron: 다음 날 새벽에 `logs/launchd-log-rotate.out.log` 빈 파일 생성 (실 회전 대상 없으면 출력 0). 30일 이상 된 로그 누적되면 `.log.gz` 출현.

---

## 📊 26단계 · 투자 결과 분석 웹 대시보드 — **✅ MVP 구현 완료** (2026-05-09)

FastAPI(JSON API) + Jinja2(서버사이드 HTML) + Chart.js/Tailwind/Alpine.js(CDN) 조합. 빌드 도구 없이 인터랙티브. 외부 호출·LLM 호출 0건.

- [x] **Phase A · MVP 3페이지** ([src/auto_trading/dashboard/](src/auto_trading/dashboard/))
  - [x] **Overview (`/`)** — 누적 자산곡선 + 일별 손익 이중축 차트, KPI 카드 4개(누적 손익/승률/매매 수/에러), 최근 7일 표.
  - [x] **Daily (`/daily`)** — 좌측 날짜 리스트 + 우측 매수·청산·시장 게이트·마감 보유 상세.
  - [x] **Backtests (`/backtests`)** — `data/backtests/<run>/` 셀렉트 → 메트릭 카드 + equity 라인 + 종목별 라운드트립 PnL 막대 + trades 테이블.
- [x] CLI: `auto-trade dashboard [--host 127.0.0.1 --port 8000 --reload --reports-root ... --backtests-root ...]`
- [x] 응답 모델·로더 read-only · path traversal 차단 · 시크릿 화이트리스트
- [x] 16개 단위/통합 테스트 (`tests/dashboard/`) — 빈 폴더, 깨진 JSON, equity.csv 없음, 잘못된 run 이름 케이스 포함
- [ ] **Phase B 확장** (별 PR · 사용자 며칠 써본 후 우선순위 정하기)
  - [ ] `/signals` — `log_parser.parse_log()` 집계, 전략·종목별 시그널 분포
  - [ ] `/insights` — 이미 만들어진 weekly insight JSON 렌더(새 LLM 호출 X)
  - [ ] 모바일 접근 (Tailscale 또는 정적 HTML 빌드)

**검증**: `uv run auto-trade dashboard` → `http://127.0.0.1:8000`. 빈 데이터 상태에서도 페이지가 죽지 않고 안내 메시지 노출.

---

## 🗞 27단계 · 주말 LLM 전략수립의 뉴스 입력 보강 — **✅ 완료** (2026-05-10)

24단계 한경 RSS 2개·30건·240자 시작 → 매체 편향 + 잘림 보강.

- [x] RSS 소스 6개 확대 (한경 finance/economy + 연합 economy/market + SBS Biz + 파이낸셜뉴스). 빈 응답 매체(이데일리·머니투데이·서울경제·매경)는 제외.
- [x] LLM 입력 캡 30→80 + `--max-news-items` 옵션, 본문 truncate 240→400자
- [x] 캐시 schema v3 + 누적 병합(url dedupe), 신규 `auto-trade fetch-news` (LLM X·비용 0)
- [x] 토요일 06:00 KST `news-prefetch` cron (plist + 셸 래퍼), README/insight-weekly 주석 갱신
- [x] 테스트 4건 추가

**비용 영향:** 호출당 ~5k→~25k input, 월 ~$0.32 → ~$2 (합의 상한 $5 이내).

---

## 🧹 28단계 · 코드베이스 리팩토링 — **✅ 완료** (9개 PR, 외부 동작·공개 API 보존)

- [x] PR2 dashboard CSV 로더 가드 → `_safe_csv_load` 데코레이터 (PEP 695 제네릭)
- [x] PR3 `_build_trade_lines` 페어링·포맷터 분해 (테스트 7건)
- [x] PR4 `paper_day` 단계별 분해 (overrides + runner wiring 헬퍼, 테스트 5건)
- [x] PR4b `weekly_proposal` 분해 → `news/cli_helpers.py` (테스트 8건)
- [x] PR5 `llm/_tool_use.py` 공통 헬퍼 — 3곳 통합 (테스트 6건, -65줄)
- [x] PR6 `notify/commands/` 패키지 분리, telegram_commands 269→208줄 (테스트 21건)
- [x] PR7 aggregator KST 경계 테스트 보강
- [x] PR8 runner `run()` 섹션 주석 + `_check_kill_switch` 헬퍼 추출 (테스트 6건)
- (PR1 conftest `isolated_cwd` 통합은 29단계 테스트 격리 후속과 통합 — 미완으로 todo-list 유지)

---

## 🛡 29단계 · 실거래 전환 안전망 — 완료 PR 아카이브

설계: `~/.claude/plans/calm-cooking-pelican.md`. 미완 항목(PR4·테스트 격리 후속)은 `todo-list.md` 에서 추적.

- [x] PR1 #9 1차 — `account/order_journal.py` (JSONL append-only) + client UUID (테스트 13건)
- [x] PR2 #9 2차 — `account/recovery.py` + 시동 시 suspect 알림 (테스트 15건)
- [x] PR3 #13 — `heartbeat.py` dead-man switch + `heartbeat-check` CLI (테스트 16건)
- [x] PR5 #10 1차 — `account/reject_classifier.py` 거절 메시지 분류 (테스트 22건)
- [x] PR6 #10 2차 — reject 분류 → 종목 skip + 알림 카테고리 (테스트 9건)
- [x] PR7 #22 — `tests/test_paper_day_full_cycle.py` mock 풀 사이클 3시나리오
- [x] PR8 #17 — `preflight.py` + `auto-trade preflight` (5 활성 게이트, 테스트 24건)
- [x] PR9 #14 1차 — `risk/consecutive_loss.py` 트래커 (테스트 8건)
- [x] PR10 #14 2차 — 연속 손절 트래커 runner wiring (테스트 12건)
- [x] PR11 #14 마무리 — `/unpause` 텔레그램 명령 (테스트 6건)
- [x] PR12 #21 — OAuth 토큰 디스크 캐시 (0o600, 테스트 8건). *PR12 토큰 캐시가 단위 테스트 격리 회귀 유발 → todo-list 후속.*
- [x] PR13 #16 1차 — `llm/cost_tracker.py` 비용 누적 (월 $5 한도, 테스트 19건)
- [x] PR14 #13 마무리 — `heartbeat-check` launchd cron (plist + install 라벨)
- [x] PR15 #16 2차 — `llm/cost.py` wiring + preflight 비용 게이트 활성 (테스트 16건)
- [x] PR16 — `orders inspect` 주문 저널 진단 명령 (테스트 6건)
- [x] PR17 #5 사전 — `detect-regime` 시장국면 진단 CLI (테스트 6건)
- [x] PR18 #5 일부 — weekly-proposal 에 시장국면 입력 추가 (테스트 6건)
- [x] PR19 #20 — `monitor/latency.py` 신호→주문 latency 추적 + CLI (테스트 15건)
- [x] PR20 #86 (2026-05-11) — kill switch `total_equity` 산식 부분익절 오발동 회귀 픽스
- [x] PR21 #87 (2026-05-11) — heartbeat 정상 종료 sentinel, cron 알림 폭주 차단
- [x] PR22 #88 (2026-05-11) — 일일 리포트 `note_ko` 매수 N건 실 체결 기준
- [x] PR27 (2026-05-12) — 운영 회귀 3건: 잔고 합계 / heartbeat 미주입 / 리포트 매수 카운트
- [x] PR43 (2026-05-14) — 운영 회귀 4건: stale 스냅샷 / 페어 라인 / in-flight SELL 가드 / 알림 강등
- [x] PR44 (2026-05-14) — 텔레그램 전 메시지 종목코드 옆 종목명 표시 (테스트 18건)
- [x] refactor — 1140줄 `runner.py` → `runner/` 패키지 (control·aggregator 분리, 동작 0 변경)

---

## 🪄 UX·기존기능 개선 배치 (2026-05-16)

설계: `~/.claude/plans/piped-sprouting-kitten.md`. 신규 기능 없이 표현·오류 안내만 개선.

- [x] **B 과매매 가시화** — 손실 핵심 원인(장중 과회전)을 처음으로 사용자 눈에 노출.
  - `reports/daily.py`: `_aggregate_notional` + `DailyReport` 에 `traded_notional`·`turnover_ratio`(분모는 `day_start_equity` 있을 때만, 추정 금지)·에러 4종 분해 필드.
  - `notify/state.py`: 요약에 거래대금·회전율, 에러 유형별 분해(0 생략), 비용 차감 PnL 값이 같아도 항상 2줄.
  - `notify/throttle.py`: 요약 헤더에 건수·분당 빈도, 분당 2건 초과 시 `⚠️ 과매매 의심`.
- [x] **A 프로파일 오류 친화화** — 신규 `cli/_profile_io.py`(독립 모듈, 순환 import·typer 경계 회피). 파일 없음/YAML 문법/비매핑/검증실패를 한국어로 안내 후 `Exit(1)`. 호출부 3곳 교체.
- [x] **D CLI 일관성** (하위호환 유지·가산만) — `backtest --from` 에 `-f`, `paper --symbols` 에 `--symbol` 별칭, 상호배타 검증을 `validate_strategy_source` 한 곳으로 통합.
- 검증: `uv run pytest` 1038 passed(신규 ~30), ruff 통과, mypy 신규 에러 0(베이스라인 20 동일).

---
