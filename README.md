# auto-stock-trading

키움증권 REST API를 이용한 Python 자동 주식 매매 봇. **모의투자(가상 돈) 기반으로 검증한 뒤에만 실거래로 넘어간다.**

코딩·주식 모두 초보를 위한 워크플로:

1. **추천**받은 전략을 **YAML 한 파일**로 저장
2. 메모장으로 **숫자만 살짝** 손봄
3. `backtest`로 과거 시뮬, `paper`로 모의투자 실시간 매매
4. (선택) **Claude API**로 주기적 방향성 조정

코드 한 줄 안 짜도 새 전략을 쓸 수 있는 게 목표다.

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
uv run auto-trade --help                      # 전체 명령 목록
uv run auto-trade quote 005930                # 현재가
uv run auto-trade balance                      # 모의 계좌 잔고
uv run auto-trade backtest …                   # 과거 일봉 시뮬
uv run auto-trade paper    …                   # 모의투자 실시간 루프 (swing)
uv run auto-trade paper-day --profile <yaml>   # day-trade 모드 (자동 발굴 + 15:15 청산)
uv run auto-trade report --date YYYY-MM-DD     # 일일 리포트 JSON 재생성
uv run auto-trade insight --days 7 --push      # (LLM) 주간 인사이트
uv run auto-trade weekly-proposal --push       # (LLM) 다음 주 day-trade 프로파일 자동 갱신
uv run auto-trade strategy suggest …           # 추천 → YAML 프로파일 저장
uv run auto-trade strategy advise  …           # (선택) LLM 으로 프로파일 조정
```

## 1) 가장 빠른 흐름 — 추천 받아서 그대로 돌려보기 (코드 X)

```bash
# 1. 종목 코드만 주면 시장국면을 분석해 어울리는 전략·파라미터를 추천
uv run auto-trade strategy suggest --symbol 005930
# → strategies/profiles/auto_005930.yaml 가 만들어진다.
#   터미널에 시장국면(추세/박스/고변동성), 추천 전략, 한국어 근거가 표로 출력됨.

# 2. (선택) 메모장으로 YAML 의 숫자만 살짝 수정
#   예: RSI period 14 → 12, oversold 30 → 25 …

# 3. 과거 일봉으로 백테스트
uv run auto-trade backtest --profile strategies/profiles/auto_005930.yaml --from 2024-01-01

# 4. 모의투자에서 실시간 운용 (안전: --dry-run 으로 주문 전송 없이 시뮬만)
uv run auto-trade paper --profile strategies/profiles/auto_005930.yaml --dry-run
```

생성되는 YAML 예시:

```yaml
# auto-trade 자동 생성 — 숫자만 살짝 바꿔도 됩니다.
# 다시 추천 받으려면: uv run auto-trade strategy suggest --symbol 005930
# backtest 실행:    uv run auto-trade backtest --profile <이 파일 경로>
# paper 실행:       uv run auto-trade paper --profile <이 파일 경로> --dry-run
name: auto_005930
symbol: "005930"
strategy: rsi_reversion        # sma_crossover 또는 rsi_reversion
params:
  period: 14                   # RSI 계산 기간 (보통 7~21)
  oversold: 30                 # 과매도 기준
  overbought: 70               # 과매수 기준
weight: 1.0                    # 0.0~1.0
notes: "박스권(±3% 변동, ATR 1.8%) → RSI 역추세"
created_at: "2026-04-26T10:15:00+09:00"
last_advised_at: null
```

### 시장국면(MarketRegime) 분류 기준 ([scanner.py](src/auto_trading/strategy/scanner.py))

| 국면 | 판정 | 추천 전략 |
| --- | --- | --- |
| `trend_up` | 일평균 기울기 ≥ +0.3% | SMA 5/20 골든크로스 (weight 1.0) |
| `trend_down` | 일평균 기울기 ≤ −0.3% | SMA 5/20 (weight 0.3 — 보수적) |
| `range` | 기울기 약함, 밴드폭 < 8% | RSI(14) 30/70 역추세 |
| `high_vol` | ATR ≥ 3% / 봉 | RSI(7) 25/75 역추세 (weight 0.5) |

임계값은 `scanner.py` 상단 상수에 모여 있어 입맛대로 조정 가능.

## 2) 직접 전략·파라미터 지정 (기존 방식)

추천을 거치지 않고 바로 실행하려면 `--strategy` + `--symbol(s)` 사용 (프로파일과 mutually exclusive).

```bash
# 백테스트
uv run auto-trade backtest --strategy sma_crossover --symbol 005930 --from 2024-01-01

# 모의투자 — 종목별 파라미터까지 콜론으로 지정 가능
uv run auto-trade paper --strategy sma_crossover \
  --symbols 005930:short=3:long=15,000660:short=5:long=20
```

내장 전략:
- **`sma_crossover`** · `short`/`long` 이동평균 골든·데드크로스 (기본 5 / 20)
- **`rsi_reversion`** · `period`/`oversold`/`overbought` Wilder RSI 역추세 (기본 14 / 30 / 70)

## 3) Claude API 로 프로파일 방향성 조정 (선택)

`.env` 에 `ANTHROPIC_API_KEY` 를 채우면 활성화. **사람이 명령으로 트리거할 때만** 호출 — cron·자동 호출은 의도적으로 막아 비용을 통제.

```bash
# 변경안 미리보기 (파일 무수정)
uv run auto-trade strategy advise --profile strategies/profiles/auto_005930.yaml --dry-run

# 실제 적용 (자동으로 .bak 백업 만들고 갱신, last_advised_at 기록)
uv run auto-trade strategy advise --profile strategies/profiles/auto_005930.yaml
```

내부 동작:
- 시스템 프롬프트는 `cache_control: ephemeral` 로 캐싱 → 매 호출 입력 토큰 절감
- 풀 OHLC 가 아니라 마지막 30봉 종가 요약(min/max/mean/std) 만 전송
- 응답은 `propose_change` tool_use 강제 → pydantic 으로 검증 후에만 YAML 갱신
- 변경 noop 이면 백업·갱신 둘 다 skip

기본 모델은 `claude-opus-4-7`. 비용을 낮추려면 `.env` 의 `ANTHROPIC_MODEL` 을 Sonnet/Haiku 계열로 바꾸면 된다.

## 4) 텔레그램 알림 + 인커밍 명령 (선택)

`.env` 의 `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` 채우면 paper 운영 중 체결·러너 정지 등을 모바일로 푸시. 추가로 `TELEGRAM_ALLOWED_CHAT_IDS` 까지 채우면 봇에 다음 명령을 보낼 수 있다 (화이트리스트 외 chat 은 무응답):

```
/status        # 모드, 종목, 전략 현황
/balance       # 예수금·평가금액·종목별 보유
/positions     # 보유 종목 표
/pnl           # 평가손익·등락률
/last_signal   # 최근 발생 시그널
```

자세한 흐름은 [todo-list.md](./todo-list.md) 의 15·16단계 참고.

## 5) Day-trade 모드 (`paper-day`) — 매일 자동 종목 발굴 + 청산

종목을 미리 정해 들고 가는 swing(`paper`) 와 달리, 매일 09:00 거래량 상위를 자동 발굴해 그날 안에 청산하는 모드. 다음날 갭 리스크가 0.

```bash
# 첫 실행 — dry-run (안전 시운전)
uv run auto-trade paper-day --top-n 3 --dry-run

# 23단계 정통 데이트레이딩 옵션 — 한도·게이트·풀 패키지
uv run auto-trade paper-day --profile strategies/profiles/daytrade-default.yaml --dry-run
```

**`--profile <yaml>` 로 활성화 가능 (모두 보수적 비활성 기본):**
- 종목별 stop-loss / 트레일링 스톱 / 부분 익절 (`PositionGuard`)
- 1회·일일 절대금액 한도 + 일일 손실 도달 시 강제 청산(kill switch)
- `IndexMomentumDayTrade` 전략 — 청산 후 쿨다운 경과 시 재진입
- KOSPI/KOSDAQ 일중 모멘텀 게이트 — 임계 통과 시만 진입
- 비용 차감 PnL 일일 리포트 (거래세 0.20% / 슬리피지 5bp / 수수료 0.015%)
- `--telegram-throttle 5` 매매 알림 5분 윈도우 요약

자세한 사용법은 [manual.md §11](./manual.md). 샘플 프로파일은 [strategies/profiles/daytrade-default.yaml](strategies/profiles/daytrade-default.yaml).

## 6) 일일 리포트 + 주간 자동화 (LLM)

`paper-day` 가 종료될 때마다 자동으로 일일 리포트가 `data/reports/daily-YYYY-MM-DD.json` 으로 저장되고 텔레그램에 푸시된다 (페어 PnL, 마감 잔고, 비용 차감 PnL 포함). 토요일 09:00 KST launchd cron 이 두 LLM 작업을 차례로 실행:

| 시점 | 동작 |
|---|---|
| 평일 08:55 | `paper-day-mode.sh` → `daytrade-current.yaml`(없으면 `daytrade-default.yaml`) 자동 로드 |
| 평일 15:15 | 일괄 청산 + 일일 리포트 텔레그램 푸시 |
| 토요일 09:00 | `auto-trade insight` (사람용 인사이트) → `auto-trade weekly-proposal` (다음 주 프로파일 자동 갱신) |

**`weekly-proposal`** 는 직전 7일 일일 리포트 + 한국경제 시장/증권 RSS 100건을 입력으로 LLM 1회 호출 → `DaytradeProposal` (안전 캡 통과한 변경만 적용) → `daytrade-current.yaml` 작성 + `.bak.YYYY-MM-DD` 백업 + 텔레그램에 변경 요약 푸시. 호출당 약 $0.04~0.08 (Opus + 캐싱), 주 1회 = 월 $0.16~0.32.

수동 호출 / 디버그:
```bash
uv run auto-trade weekly-proposal --dry-run   # LLM 호출 없이 입력 요약만
uv run auto-trade weekly-proposal --push       # 풀 흐름 + 텔레그램 푸시
```

자동화 설정은 [scripts/launchd/README.md](./scripts/launchd/README.md) 참고.

## 테스트

```bash
uv run pytest                       # 단위 테스트 (네트워크 X)
uv run pytest -m integration        # 모의투자 실 API 호출 (.env 필요)
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy
```

## 진행 상황

[todo-list.md](./todo-list.md) 에서 초보자 친화 설명과 함께 단계별 체크리스트 확인.

## 안전장치

- 기본 `KIWOOM_MODE=mock`. 실거래 전환은 `KIWOOM_MODE=real` + `--allow-real` 둘 다 있어야 동작.
- 모든 주문 경로에 [risk/manager.py](src/auto_trading/risk/manager.py) 가 종목 20% / 포트폴리오 80% / 일일 손실 3% 한도를 강제.
- 23단계 추가 한도 (모두 보수적 비활성 기본): 종목별 stop/트레일링/부분익절 ([risk/position_guard.py](src/auto_trading/risk/position_guard.py)), 1회·일일 절대금액 한도, kill switch (일일 손실 도달 시 강제 청산).
- LLM 자동 갱신([news/profile_writer.py](src/auto_trading/news/profile_writer.py)) 은 안전 캡(top_n ≤ 5, stop_loss ≤ 3%, trailing/partial 등) 통과한 변경만 적용 + `.bak` 백업.
- LLM 호출 빈도: 사람 명시 트리거 또는 사전 합의된 주 1회 cron 만. 매일·매시간 호출 금지.
- `paper --dry-run` 으로 주문 전송 없이 전략·리스크 로직만 검증 가능.
- 시크릿(`.env`, AppKey/Secret, 토큰) 은 git 제외 + 로그 마스킹.
