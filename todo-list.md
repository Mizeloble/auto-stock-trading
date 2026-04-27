# 할 일 체크리스트 (초보자 가이드)

각 항목은 **무엇을 만드는지 / 왜 필요한지 / 어떻게 확인하는지** 세 줄로 설명합니다.

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
- [ ] 모의 키 세팅 후 **장중 시간** 에 `uv run auto-trade paper -s sma_crossover --symbols 005930` 실행 → 로그에 `ws.connected`, `ws.subscribe`, `runner.bar` 가 쌓이면 정상.

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

- [ ] **첫 등록**: `./scripts/launchd-install.sh` → `launchctl list | grep mizeloble.auto-trade` 두 줄 보이면 성공.
- [ ] **강제 즉시 시동(장 외)**: `launchctl kickstart -k gui/$UID/com.mizeloble.auto-trade.paper` → 텔레그램 시작 푸시 도착하면 시동 OK. `kickstart -k …paper-stop` 으로 정리.
- [ ] **첫 자동 평일 운영**: 다음 평일 09:00 직전 폰에서 시작 푸시 + `/status` 응답 정상이면 통과. `logs/paper-YYYY-MM-DD.log` 에 `runner.bar` 누적 확인.
- [ ] **회사에서 v3 명령 검증**: 폰에서 `/pause` → 응답 + 다음 신호에서 `runner.order_skipped_paused` 로그. `/resume` 으로 복귀 후 `/stop` 으로 graceful 종료.

---

## 🚀 남은 과제 (선택)

- **인디케이터 캐시** · `data/cache/<symbol>_daily.parquet` 으로 차트 API 호출 절약.
- **클라우드 이전** · 며칠 launchd 운영 후 가치 확인되면 Oracle 무료 티어/Hetzner 로 systemd 이전. `--dry-run` 그대로 검증 후 떼면 실거래 후보.
- **실거래 전환** · `.env` 에 실계좌 키 + `KIWOOM_MODE=real` + `--allow-real` (사용자 명시 승인 후).
