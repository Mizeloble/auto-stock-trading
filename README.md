# auto-stock-trading

키움증권 REST API를 이용한 Python 자동 주식 매매 봇. **모의투자(가상 돈) 기반으로 검증한 뒤에만 실거래로 넘어간다.**

운영 모드는 **day-trade(`paper-day`) 가 메인** — 매일 09:00 거래량 상위 종목을 자동 발굴해 그날 안에 청산하고, 토요일 LLM cron 이 다음 주 프로파일을 자동 갱신한다. 미리 정한 종목으로 며칠 보유하는 swing(`paper`) 모드는 학습·단일 종목 검증용으로 남겨 둔다.

코드 한 줄 안 짜도 새 전략을 쓸 수 있는 게 목표.

## 설치

```bash
uv sync          # .venv 생성 + 의존성 설치 (Python 3.14)
cp .env.example .env
# .env 파일을 열어 KIWOOM_APP_KEY / KIWOOM_APP_SECRET 에 모의투자 키를 입력
```

> 모의투자 AppKey 는 [키움 개발자 센터](https://openapi.kiwoom.com) 에서 모의투자 신청 **후** 발급. 실거래 키와 별개라 8030 에러 만나면 모의투자용 키로 재발급 필요.

`.env` 의 주요 항목 (전부 선택 — 채운 만큼만 기능이 켜진다):

| 변수 | 용도 |
| --- | --- |
| `KIWOOM_MODE=mock` | `mock`(기본) / `real`. 실거래 전환은 사용자 명시 승인 후에만 |
| `KIWOOM_APP_KEY` / `KIWOOM_APP_SECRET` / `KIWOOM_ACCOUNT_NO` | 모의투자 키 |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | (선택) 체결·러너 정지 알림 푸시 |
| `TELEGRAM_ALLOWED_CHAT_IDS` | (선택) 인커밍 명령 허용 chat_id (콤마 구분). 비우면 봇은 푸시만 |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` | (선택) `strategy advise` 가 LLM 으로 프로파일 조정. 사람이 명령으로 트리거할 때만 호출 |

## 명령 한눈에 보기

```bash
uv run auto-trade --help                       # 전체 명령 목록
uv run auto-trade quote 005930                 # 현재가
uv run auto-trade balance                      # 모의 계좌 잔고

# day-trade (메인)
uv run auto-trade paper-day --profile <yaml>   # 자동 발굴 + 15:15 청산
uv run auto-trade report --date YYYY-MM-DD     # 일일 리포트 JSON 재생성
uv run auto-trade insight --days 7 --push      # (LLM) 주간 인사이트
uv run auto-trade fetch-news --days 7          # RSS 캐시만 갱신 (LLM X · 비용 0)
uv run auto-trade weekly-proposal --push       # (LLM) 다음 주 day-trade 프로파일 자동 갱신
uv run auto-trade dashboard                    # 결과 분석 웹 대시보드 (로컬 127.0.0.1:8000)

# swing (선택 · 학습/검증용)
uv run auto-trade strategy suggest …           # 추천 → YAML 프로파일 저장
uv run auto-trade backtest …                   # 과거 일봉 시뮬
uv run auto-trade paper …                      # 단일 종목 실시간 루프
uv run auto-trade strategy advise …            # (LLM) 프로파일 조정
```

## 1) Day-trade 모드 (`paper-day`) — 메인 운영 흐름

매일 09:00 거래량 상위를 자동 발굴해 그날 안에 청산하는 모드. 다음날 갭 리스크가 0. 매주 토요일 LLM 이 다음 주 프로파일을 자동 갱신.

```bash
# 첫 실행 — dry-run (안전 시운전)
uv run auto-trade paper-day --top-n 3 --dry-run

# 정통 데이트레이딩 옵션 — 한도·게이트·풀 패키지
uv run auto-trade paper-day --profile strategies/profiles/daytrade-default.yaml --dry-run
```

**`--profile <yaml>` 로 활성화. 기본 프로파일(`daytrade-default.yaml`) 은 위험 통제 4종(stop_loss / trailing / partial_take / daily_loss_kill) 활성, 모멘텀 게이트는 비활성으로 시작:**
- 종목별 stop-loss / 트레일링 스톱 / 부분 익절 (`PositionGuard`)
- 1회·일일 절대금액 한도 + 일일 손실 도달 시 강제 청산(kill switch)
- `IndexMomentumDayTrade` 전략 — 청산 후 쿨다운 경과 시 재진입
- KOSPI/KOSDAQ 일중 모멘텀 게이트 — 임계 통과 시만 진입
- 비용 차감 PnL 일일 리포트 (거래세 0.20% / 슬리피지 5bp / 수수료 0.015%)
- `--telegram-throttle 5` 매매 알림 5분 윈도우 요약

자세한 사용법은 [manual.md §11](./manual.md). 샘플 프로파일은 [strategies/profiles/daytrade-default.yaml](strategies/profiles/daytrade-default.yaml).

## 2) 일일 리포트 + 주간 자동화 (LLM)

`paper-day` 가 종료될 때마다 자동으로 일일 리포트가 `data/reports/daily-YYYY-MM-DD.json` 으로 저장되고 텔레그램에 푸시된다 (페어 PnL, 마감 잔고, 비용 차감 PnL 포함). 토요일 09:00 KST launchd cron 이 두 LLM 작업을 차례로 실행:

| 시점 | 동작 |
|---|---|
| 평일 08:55 | `paper-day-mode.sh` → `daytrade-current.yaml`(없으면 `daytrade-default.yaml`) 자동 로드 |
| 평일 15:15 | 일괄 청산 + 일일 리포트 텔레그램 푸시 |
| 토요일 06:00 | `auto-trade fetch-news` (RSS 캐시 선반영, LLM X) — 27단계 |
| 토요일 09:00 | `auto-trade insight` (사람용 인사이트) → `auto-trade weekly-proposal` (다음 주 프로파일 자동 갱신) |

**`weekly-proposal`** 는 직전 7일 일일 리포트 + 6개 RSS(한경 finance·economy, 연합뉴스 economy·market, SBS Biz, 파이낸셜뉴스) 뉴스를 입력으로 LLM 1회 호출 → `DaytradeProposal` (안전 캡 통과한 변경만 적용) → `daytrade-current.yaml` 작성 + `.bak.YYYY-MM-DD` 백업 + 텔레그램에 변경 요약 푸시. LLM 입력은 상위 80건(`--max-news-items`) · 본문 400자 truncate. 호출당 약 $0.4~0.5 (Opus + 캐싱), 주 1회 = 월 약 $2 (상한 $5).

토요일 06:00 `news-prefetch` cron 이 RSS 를 미리 fetch 해 캐시에 쌓고, 09:00 호출은 `--force-refresh` default 로 그 사이 새 뉴스를 url 키 dedupe 후 누적 병합 → 미국 시장 마감 직후의 한국어 보도까지 포함.

수동 호출 / 디버그:
```bash
uv run auto-trade fetch-news --days 7              # RSS 캐시만 갱신 (LLM X)
uv run auto-trade weekly-proposal --dry-run        # LLM 호출 없이 입력 요약만
uv run auto-trade weekly-proposal --max-news-items 100 --push  # 더 많은 뉴스로 풀 흐름
```

자동화 설정은 [scripts/launchd/README.md](./scripts/launchd/README.md) 참고.

## 3) (선택) Swing 모드 — 단일 종목 학습·검증

미리 정한 종목으로 며칠 보유하는 흐름. 정기 운영에서는 빠졌고, 새 전략·새 종목을 backtest 와 dry-run 으로 감 잡고 싶을 때 사용한다.

```bash
# 1. 시장국면 분석 → 어울리는 전략·파라미터 추천 → strategies/profiles/auto_005930.yaml
uv run auto-trade strategy suggest --symbol 005930

# 2. 과거 일봉 백테스트
uv run auto-trade backtest --profile strategies/profiles/auto_005930.yaml --from 2024-01-01

# 3. 모의투자 실시간 — --dry-run 으로 주문 전송 없이 전략·리스크만 검증
uv run auto-trade paper --profile strategies/profiles/auto_005930.yaml --dry-run

# 4. (선택) Claude 가 YAML 숫자 조정 (사람 명시 트리거 only)
uv run auto-trade strategy advise --profile strategies/profiles/auto_005930.yaml --dry-run
```

`strategy suggest` 의 시장국면(MarketRegime) 분류 ([scanner.py](src/auto_trading/strategy/scanner.py)):

| 국면 | 판정 | 추천 전략 |
| --- | --- | --- |
| `trend_up` | 일평균 기울기 ≥ +0.3% | SMA 5/20 골든크로스 (weight 1.0) |
| `trend_down` | 일평균 기울기 ≤ −0.3% | SMA 5/20 (weight 0.3 — 보수적) |
| `range` | 기울기 약함, 밴드폭 < 8% | RSI(14) 30/70 역추세 |
| `high_vol` | ATR ≥ 3% / 봉 | RSI(7) 25/75 역추세 (weight 0.5) |

추천을 거치지 않고 직접 호출하려면 `--strategy` + `--symbol(s)` (프로파일과 mutually exclusive). 내장 전략은 **`sma_crossover`**(`short`/`long` 기본 5/20) 와 **`rsi_reversion`**(`period`/`oversold`/`overbought` 기본 14/30/70). `strategy advise` 는 마지막 30봉 종가 요약만 LLM 에 보내고 `propose_change` tool_use 강제로 검증 후 YAML 갱신, 변경 noop 이면 백업·갱신 둘 다 skip.

## 4) (선택) 텔레그램 알림 + 인커밍 명령

`.env` 의 `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` 채우면 paper / paper-day 운영 중 체결·러너 정지·청산 등을 모바일로 푸시. 추가로 `TELEGRAM_ALLOWED_CHAT_IDS` 까지 채우면 봇에 다음 명령을 보낼 수 있다 (화이트리스트 외 chat 은 무응답):

```
/status        # 모드, 종목, 전략 현황
/balance       # 예수금·평가금액·종목별 보유
/positions     # 보유 종목 표
/pnl           # 평가손익·등락률
/last_signal   # 최근 발생 시그널
```

자세한 흐름은 [todo-list.md](./todo-list.md) 의 15·16단계 참고.

## 테스트

```bash
uv run pytest                       # 단위 테스트 (네트워크 X)
uv run pytest -m integration        # 모의투자 실 API 호출 (.env 필요)
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy
```

## 진행 상황

27단계(주말 LLM 뉴스 입력 6개 RSS · 80건 cap · 400자 truncate · 토요일 06:00 prefetch cron)까지 반영. 28단계 코드 정리(테스트 픽스처 통합 / dashboard CSV 가드 / notify·llm·cli 분해 / runner 가독성 + 단위 테스트 보강 9개 PR) 머지 완료 — 외부 동작·공개 API 변경 없음.

단계별 상세 체크리스트는 [todo-list.md](./todo-list.md), 1~26단계 이력은 [docs/history.md](./docs/history.md).

## 안전장치

- 기본 `KIWOOM_MODE=mock`. 실거래 전환은 `KIWOOM_MODE=real` + `--allow-real` 둘 다 있어야 동작.
- 모든 주문 경로에 [risk/manager.py](src/auto_trading/risk/manager.py) 가 종목 20% / 포트폴리오 80% / 일일 손실 3% 한도를 강제.
- 23단계 추가 한도: 종목별 stop/트레일링/부분익절 ([risk/position_guard.py](src/auto_trading/risk/position_guard.py)), 1회·일일 절대금액 한도, kill switch (일일 손실 도달 시 강제 청산). `daytrade-default.yaml` 에서 위험 통제 4종 기본 활성, 절대금액 캡과 모멘텀 게이트는 비활성(자본 규모·시장 안정도에 따라 사용자 또는 LLM 이 켬).
- LLM 자동 갱신([news/profile_writer.py](src/auto_trading/news/profile_writer.py)) 은 안전 캡(top_n ≤ 5, stop_loss ≤ 3%, trailing/partial 등) 통과한 변경만 적용 + `.bak` 백업.
- LLM 호출 빈도: 사람 명시 트리거 또는 사전 합의된 주 1회 cron 만. 매일·매시간 호출 금지.
- `paper-day` / `paper` 모두 `--dry-run` 기본. 주문 전송 없이 전략·리스크 로직만 검증 가능.
- 시크릿(`.env`, AppKey/Secret, 토큰) 은 git 제외 + 로그 마스킹.
