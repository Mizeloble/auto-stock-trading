# 초심자 매뉴얼 — auto-stock-trading

> 코딩도, 주식도 처음이라도 따라올 수 있게 만든 매뉴얼입니다.
> 모든 명령은 **터미널(맥은 Terminal, 윈도우는 PowerShell)** 에 그대로 복사·붙여넣기 하면 됩니다.
> 절대 실수해서는 안 되는 단 한 가지: **`KIWOOM_MODE=mock`(모의투자) 로 시작하기**. 진짜 돈은 충분히 검증한 뒤에만 씁니다.

---

## 0. 이 시스템이 뭔가요?

키움증권의 **REST API**(증권사 서버에 명령을 보내는 인터넷 통로) 를 통해
**Python 프로그램이 사람 대신 종목을 사고 파는 자동매매 봇** 입니다.

흐름을 그림으로 풀면:

```
[키움 서버] ⇄ (REST·WebSocket) ⇄ [auto-trade 프로그램] → [전략 판단] → [리스크 검사] → [주문 전송]
                                              ↑
                                      [추천기 / Claude 조언자]
                                              ↓
                                    [텔레그램 알림 · 명령]
```

봇이 하는 일을 요약하면:

1. **시세 받기** — 1분마다 또는 실시간으로 가격을 받음
2. **전략으로 판단** — "지금 사라/팔라/가만히 있어라" 신호 생성
3. **리스크 매니저 검사** — 한 종목에 너무 많이 쏠리지 않게 컷
4. **주문** — 모의투자 계좌에 매수/매도 전송
5. **알림** — (선택) 텔레그램으로 체결 결과 푸시

---

## 1. 사전 준비물 (한 번만 하면 됨)

| 준비물 | 어디서 | 비고 |
|---|---|---|
| 키움증권 계좌 | 키움 앱/홈페이지 | 실계좌 1개 필요 (모의투자 신청용) |
| 모의투자 신청 | [openapi.kiwoom.com](https://openapi.kiwoom.com) | "모의투자" 메뉴에서 신청 |
| 모의투자 AppKey/Secret | 같은 사이트에서 발급 | **실거래 키와 다름!** 헷갈리면 8030 에러 |
| Python 3.14 | [python.org](https://www.python.org) | 또는 `uv` 가 자동 설치 |
| `uv` (패키지 도구) | `curl -LsSf https://astral.sh/uv/install.sh \| sh` | 맥/리눅스. 윈도우는 사이트 가이드 참조 |
| 텔레그램 봇 (선택) | @BotFather 에게 `/newbot` | 알림 받고 싶을 때만 |
| Anthropic API 키 (선택) | [console.anthropic.com](https://console.anthropic.com) | Claude 로 전략 조언 받고 싶을 때만 |

**무엇** — 위 준비물은 봇이 "내 모의투자 계좌"에 접속하기 위한 신분증입니다.
**왜** — AppKey/Secret 없이는 키움 서버가 우리 명령을 거절합니다.
**확인** — 키움 개발자센터에 로그인해서 "모의투자 → APP 관리" 에서 키 두 개가 보이면 OK.

---

## 2. 설치 — 5분이면 끝

```bash
# 1) 코드 받기
git clone <이 저장소 URL>
cd auto-stock-trading

# 2) 의존성 설치 (uv 가 .venv 자동 생성)
uv sync

# 3) 환경설정 파일 만들기
cp .env.example .env
```

이제 `.env` 파일을 메모장(맥은 TextEdit, 윈도우는 메모장, VSCode 도 OK) 으로 열어 아래 4개 줄만 채웁니다:

```env
KIWOOM_MODE=mock                  # 절대 real 로 바꾸지 마세요!
KIWOOM_APP_KEY=발급받은_AppKey
KIWOOM_APP_SECRET=발급받은_AppSecret
KIWOOM_ACCOUNT_NO=모의투자_계좌번호
```

**왜 `mock` 이어야 하나요?**
`mock` = 모의투자 = 가상의 돈. 실수해도 한 푼도 안 날아갑니다.
`real` = 실거래 = 진짜 내 돈. 코드가 100% 검증되기 전엔 절대 켜지 않습니다.

**확인**:
```bash
uv run auto-trade quote 005930
```
삼성전자(005930) 현재가가 표로 출력되면 설치·인증 성공입니다.
`KIWOOM_AUTH_FAILED` 류 에러가 나면 키 오타 또는 모의투자 미신청 가능성 → 키움 개발자센터에서 키 재확인.

---

## 3. 명령 한 줄짜리 사용법

| 명령 | 무엇을 하나 |
|---|---|
| `uv run auto-trade --help` | 전체 명령어 목록 |
| `uv run auto-trade quote 005930` | 한 종목 현재가 보기 |
| `uv run auto-trade balance` | 모의 계좌 잔고/평가금액 |
| `uv run auto-trade strategy suggest --symbol 005930` | 봇이 그 종목에 어울리는 전략을 **추천**하고 YAML 저장 |
| `uv run auto-trade backtest --profile <yaml> --from 2024-01-01` | 과거 데이터로 시뮬레이션 |
| `uv run auto-trade paper --profile <yaml> --dry-run` | 실시간으로 동작은 하되 **주문은 안 보냄** (안전 시운전) |
| `uv run auto-trade paper --profile <yaml>` | 실시간 모의투자 매매 (주문 전송 ON, 단 모의계좌이므로 가상 돈) |
| `uv run auto-trade paper-day --top-n 3 --dry-run` | **day-trade 모드**: 매일 거래량 상위 3종목 자동 선정 → 즉시 매수 → 15:15 일괄 청산. 자세히는 §11. |
| `uv run auto-trade paper-day --profile strategies/profiles/daytrade-default.yaml --dry-run` | **23단계 정통 데이트레이딩**: PositionGuard / 다회 진입 / 모멘텀 게이트 / kill switch / 비용 차감 PnL. 자세히는 §11. |
| `uv run auto-trade report --date YYYY-MM-DD --push` | 그날 paper-day 로그 → 일일 리포트 JSON + 텔레그램 푸시 (수동 재생성). |
| `uv run auto-trade insight --days 7 --push` | (선택) 직전 7일 일일 리포트를 LLM 분석 → 한국어 인사이트 푸시. ANTHROPIC_API_KEY 필요. |
| `uv run auto-trade weekly-proposal --dry-run` | (선택) 24단계 — 다음 주 day-trade 프로파일 자동 갱신 시뮬. 토요일 cron 이 자동 호출. |
| `uv run auto-trade strategy advise --profile <yaml>` | (선택) Claude 가 YAML 의 숫자를 조언으로 갱신 |

> 윈도우 PowerShell 에서 줄을 `\` 로 이어 적을 땐 `` ` `` 백틱을 쓰세요.

---

## 4. "코드 한 줄 안 짜고" 새 전략 굴리는 흐름 (가장 권장)

```
①  추천 받기   → ②  YAML 살짝 손보기   → ③  백테스트   → ④  paper --dry-run   → ⑤  paper 실전(모의)
```

### ① 추천 받기

```bash
uv run auto-trade strategy suggest --symbol 005930
```

**무엇** — 최근 60봉(약 3개월)의 가격 패턴을 분석해 "추세 상승 / 하락 / 박스권 / 고변동" 중 어디인지 분류하고 그에 맞는 전략·파라미터를 골라 줍니다.
**왜** — 초보자는 "어떤 전략을 써야 하지?" 가 가장 어렵기 때문입니다. 시장 국면에 어울리는 기본값을 자동으로 잡아 줍니다.
**확인** — 터미널에 표가 뜨고, `strategies/profiles/auto_005930.yaml` 이 만들어집니다.

생성된 YAML 예시:
```yaml
name: auto_005930
symbol: "005930"
strategy: rsi_reversion        # sma_crossover 또는 rsi_reversion
params:
  period: 14                   # RSI 기간 (보통 7~21)
  oversold: 30                 # 이 값보다 낮으면 매수
  overbought: 70               # 이 값보다 높으면 매도
weight: 1.0                    # 신호 강도 (0.0~1.0)
notes: "박스권(±3% 변동, ATR 1.8%) → RSI 역추세"
```

### ② YAML 살짝 손보기 (선택)

메모장으로 열어 `period: 14` → `12` 같이 **숫자만** 바꿉니다.
**주의**: 들여쓰기(빈칸 두 개)는 절대 건드리면 안 됩니다. YAML 은 들여쓰기로 의미를 구분합니다.

### ③ 백테스트 (과거 데이터 시뮬)

```bash
uv run auto-trade backtest --profile strategies/profiles/auto_005930.yaml --from 2024-01-01
```

**무엇** — "이 전략을 작년 1월 1일부터 오늘까지 돌렸으면 얼마 벌었을까?" 를 계산.
**왜** — 실시간 돈을 쓰기 전, 숫자로 검증하기 위함.
**확인** — 종합 수익률·승률·최대낙폭(MDD) 표가 뜨고 `data/backtests/...` 에 결과가 저장됩니다. **수익률이 마이너스이거나 MDD가 너무 크면(>20%) 다른 전략을 시도**.

### ④ paper --dry-run (안전 시운전)

```bash
uv run auto-trade paper --profile strategies/profiles/auto_005930.yaml --dry-run
```

**무엇** — 실시간 시세를 받으면서 전략·리스크 로직을 모두 돌리지만, **주문 전송만 건너뜁니다**.
**왜** — 진짜 모의투자 계좌에 주문을 쏘기 전, "신호가 너무 자주 나오는지", "원하지 않는 시점에 사고팔려 하는지" 를 로그로 점검.
**확인** — 터미널 로그에 `[DRY] would place BUY ...` 같은 가짜 주문이 보이면 OK. 실제 잔고는 변하지 않습니다.

### ⑤ 실전(모의) 매매

```bash
uv run auto-trade paper --profile strategies/profiles/auto_005930.yaml
```

이제 모의투자 계좌에 진짜로 매수·매도가 들어갑니다 (가상 돈이지만).
장 마감(15:30) 까지 알아서 돌고, `Ctrl+C` 로 멈출 수 있습니다.

---

## 5. 내장 전략 세 가지

### 5-1. SMA 골든크로스 (`sma_crossover`)

- **무엇** — 단기 이동평균(예: 5일)이 장기 이동평균(예: 20일) 을 위로 뚫으면 매수, 아래로 뚫으면 매도.
- **언제 잘 통하나** — 한 방향으로 추세가 분명할 때 (상승/하락 추세장).
- **파라미터** — `short` (기본 5), `long` (기본 20).

### 5-2. RSI 역추세 (`rsi_reversion`)

- **무엇** — RSI 가 30 미만으로 떨어지면 "너무 팔렸다" → 매수, 70 초과면 "너무 샀다" → 매도.
- **언제 잘 통하나** — 박스권(가격이 위아래로 왔다갔다) 일 때.
- **파라미터** — `period` (기본 14), `oversold` (기본 30), `overbought` (기본 70).

### 5-3. 당일 모멘텀 (`intraday_momentum`) — day-trade 전용

- **무엇** — 발굴된 종목의 첫 봉이 들어오는 순간 BUY 시그널 한 번만 발사. 이후 침묵.
- **언제 잘 통하나** — day-trade 모드(§11) 의 균등 매수에만 사용. 직접 호출은 비권장.
- **파라미터** — `weight` (기본 1.0). orchestrator 가 자동으로 1/N 로 분할 주입.

> 세 전략 모두 `Signal` 만 반환합니다 — 실제 주문은 다음 단계의 **리스크 매니저** 가 결정합니다.

---

## 6. 안전장치 (리스크 매니저)

자동으로 적용되는 한도들 (보수적 기본값):

| 한도 | 기본값 | 효과 |
|---|---|---|
| 종목당 최대 비중 | 예수금의 **20%** | 한 종목에 몰빵 방지 |
| 포트폴리오 총 익스포저 | 예수금의 **80%** | 항상 일부 현금 보유 |
| 일일 손실 한도 | -**3%** | 그날 -3% 도달 시 신규 매수 정지 |

**왜** — 초보가 가장 자주 잃는 패턴은 "한 종목에 다 넣고 손실 누적" 입니다. 한도가 자동으로 끊어 줍니다.
**확인** — 신호가 나도 한도에 걸리면 로그에 `risk_reject reason=...` 이 남습니다.

이 한도들은 `src/auto_trading/risk/manager.py` 에 정의돼 있고, **사용자가 명시적으로 승인할 때만** 완화합니다.

---

## 7. 모드 비교 — 절대 헷갈리면 안 됨

| 항목 | `KIWOOM_MODE=mock` | `KIWOOM_MODE=real` |
|---|---|---|
| 돈 | 가상 (모의투자) | **진짜** |
| 키 | 모의투자 AppKey | 실거래 AppKey (다른 키!) |
| paper 명령 | 그냥 동작 | 추가로 `--allow-real` 플래그 필요 |
| 권장 단계 | 항상 여기서 시작 | 모의에서 수개월 검증 후 |

`real` 로 바꾸려면 **(a)** `.env` 의 `KIWOOM_MODE=real` 로 수정하고 **(b)** 명령에 `--allow-real` 을 직접 입력해야 합니다. 두 개가 동시에 충족되지 않으면 봇이 자동으로 거부합니다.

---

## 8. (선택) 텔레그램 알림 + 원격 명령

폰으로 체결 결과를 받고, 답장으로 봇을 제어할 수 있습니다.

**무엇이 푸시되나** — 매수/매도 체결, 러너 시작/종료, 에러.
**왜** — 회사·외출 중에도 상태 확인 가능.

설정:
1. 텔레그램에서 `@BotFather` 검색 → `/newbot` → 토큰 받기
2. 본인 봇과 대화 시작 → `https://api.telegram.org/bot<TOKEN>/getUpdates` 접속해서 `chat.id` 확인
3. `.env` 에 입력:
```env
TELEGRAM_BOT_TOKEN=받은_토큰
TELEGRAM_CHAT_ID=내_chat_id
TELEGRAM_ALLOWED_CHAT_IDS=내_chat_id   # 명령도 받고 싶다면
```

봇에 보낼 수 있는 명령:

| 명령 | 효과 |
|---|---|
| `/status` | 모드·종목·전략 현황 |
| `/balance` | 예수금·평가금액·보유 종목 |
| `/positions` | 보유 종목 표 |
| `/pnl` | 평가손익·등락률 |
| `/last_signal` | 최근 신호 |

`TELEGRAM_ALLOWED_CHAT_IDS` 화이트리스트 외 chat 은 무응답 → 보안상 안전.

---

## 9. (선택) Claude 로 전략 조언 받기

```bash
# 변경안만 미리보기 (파일 수정 안 함)
uv run auto-trade strategy advise --profile strategies/profiles/auto_005930.yaml --dry-run

# 실제 적용 (.bak 백업 자동 생성)
uv run auto-trade strategy advise --profile strategies/profiles/auto_005930.yaml
```

**무엇** — 최근 30봉의 요약(min/max/mean/std) 만 Claude 에게 보내고, "지금 시장 국면에 어울리게 전략·파라미터·비중을 조정해줘" 라고 부탁합니다.
**왜** — 사람이 매번 차트 보고 숫자 조정하기 귀찮을 때.
**비용 통제** — **사람이 명령을 칠 때만** 호출됩니다 (cron/자동 호출은 의도적으로 차단). `.env` 의 `ANTHROPIC_MODEL` 을 Sonnet/Haiku 로 바꾸면 더 저렴.

---

## 10. (선택) 매일 자동으로 켜고 끄기 — macOS launchd

회사 가 있을 때도 **08:55 자동 시작 / 15:15 자동 청산 → 종료** 로 돌릴 수 있습니다. 권장 모드는 **day-trade** (§11) — `./scripts/launchd-install.sh` 한 줄로 paper-day · insight-weekly · log-rotate 세 잡이 함께 등록됩니다.

```bash
./scripts/launchd-install.sh
launchctl list | grep mizeloble.auto-trade   # paper-day / insight-weekly / log-rotate 3종 보이면 OK
```

> swing 모드(미리 정한 종목으로 며칠 보유)는 자동화 plist 가 deprecated 처리되어 `scripts/legacy/` 로 이동했습니다. 수동으로는 여전히 `uv run auto-trade paper --profile <yaml> --dry-run` 가능. 복귀 절차는 [scripts/legacy/README.md](scripts/legacy/README.md).

자세한 내용·해제 방법은 [scripts/launchd/README.md](scripts/launchd/README.md) 참고.

---

## 11. (선택) Day-trade 모드 — 매일 자동 종목 발굴

**무엇** — 종목을 미리 정해 들고 가는 swing(`paper`) 와 달리, 매일 아침 거래량 상위 종목을 자동으로 골라 그날 안에 청산하는 모드.

**왜 쓰나** — 다음날 갭 리스크(밤사이 호재/악재로 갑자기 가격이 튀거나 빠지는 위험) 를 0 으로 만들고 싶을 때. 시장 변동성을 그날그날 따라감.

### 흐름 한눈에

```
09:00      paper-day 시동 → 텔레그램 시작 푸시
09:00:30   거래량 상위 N종목 발굴 (KOSPI+KOSDAQ 통합)
           → ETF/ETN/SPAC 자동 제외 (인버스 ETF 가 거래량 상위 자주 점령)
           → 종목 소속 시장(KOSPI/KOSDAQ) 5/20일선 게이트 통과 종목만
09:00~     첫 봉에 균등 분할 매수 (weight=1/N, 리스크 매니저가 종목당 20% 캡)
15:15      보유 전부 시장가 매도 → graceful 종료
           → 텔레그램 청산 완료 + 종료 푸시
```

### 첫 실행 — dry-run (안전 시운전)

```bash
uv run auto-trade paper-day --top-n 3 --dry-run
```

- `--top-n 3` : 후보 종목 수 (3종목 권장 — 리스크 매니저 캡과 잘 맞고 모니터링 부담 적음)
- `--dry-run` : 실제 주문 안 보냄. 시그널/리스크 로직만 검증.
- `--liquidate 15:15` : 청산 시각 (기본 15:15 KST. 정규장 마감 15:30 직전).
- `--include-etf` : (선택) ETF/ETN/SPAC 도 매수 대상에 포함. 기본은 제외 (일반 주식 day-trade 의도).
- `--discover-delay 30` : 09:00 직후 거래량 데이터 안정화 대기 초. 빠른 검증 시 0.
- `--profile <yaml>` : (23단계) DaytradeProfile YAML 로드. 지정 시 strategy/top_n/liquidate/RiskLimits 가 프로파일 값으로 덮어써짐. 샘플: [strategies/profiles/daytrade-default.yaml](strategies/profiles/daytrade-default.yaml).
- `--telegram-throttle 5` : (23단계) 매매 알림 5분 윈도우 요약. 다회 진입 시 채터링 방지. 0=비활성.

### 결과 해석 — 로그 키워드

| 로그 키 | 의미 |
|---|---|
| `runner.awaiting_universe` | 발굴 대기 중. 09:00:30 까지 기다림. |
| `market.rank.top_volume excluded_count=N` | 거래량 상위에서 ETF/ETN/SPAC N개 자동 제외됨. |
| `market.index.gate above=true/false` | 시장 5/20일선 게이트 결과. False 면 그 시장 종목 매수 차단. |
| `daytrade.universe_chosen symbols=[...]` | 게이트 통과해 매수 대상이 된 종목들. |
| `runner.order_simulated side=buy ...` | dry-run 매수 시뮬 (×N). |
| `runner.liquidate_simulated side=sell ...` | 15:15 dry-run 청산 시뮬. |
| `runner.stopped` | graceful 종료 완료. |

### 텔레그램 알림 (설정돼 있으면)

- `🔎 발굴 완료 · 매수 후보 N건` (선정/차단/메모)
- `✅ 체결 접수` (매수 N건)
- `🧹 청산 완료 (dry-run) · N종목`
- `⏹ paper 정지 (정상 종료)`

`/status` 보내면 `▶ 운영중 · day-trade (청산 15:15)` 처럼 모드 표시.

### 매수가 안 들어가는 경우

- **모든 후보가 게이트 차단**: 약세장(시장 5/20일선 아래) 이라 보호 차원. **매수 0** + 즉시 종료. 정상.
- **발굴 결과 0건**: 키움 응답 이상 — 로그 `daytrade.discovery_failed` 확인.
- 스크린샷 예시: 5/3 mock 데이터에서 KOSPI/KOSDAQ 둘 다 5일선 아래 → 후보 3건 모두 차단 → 매수 없이 종료.

### swing vs day-trade 결정

| 항목 | swing (`paper`) | day-trade (`paper-day`) |
|---|---|---|
| 종목 결정 | YAML 에 미리 명시 | 매일 09:00 자동 발굴 |
| 매수 시점 | 전략 신호 발생 시 | 발굴 직후 즉시 균등 매수 |
| 청산 | 외부 stop 시각 (다음날도 보유 가능) | 15:15 자동 일괄 매도 |
| 다음날 갭 리스크 | 있음 | **없음** (당일 청산) |
| 적합한 사람 | 종목 분석 좋아함, 며칠 보유 OK | 종목 고르기 귀찮음, 갭 리스크 싫음 |

> 처음엔 둘 다 dry-run 으로 일주일씩 돌려보고 어느 쪽이 본인 성향에 맞는지 보세요. 동시 운영은 권장 X (launchd plist 충돌).

### 23단계 — 정통 데이트레이딩 옵션 + YAML 프로파일

23단계 머지 후 `paper-day --profile <yaml>` 로 다음을 한 번에 활성화. 기본 `daytrade-default.yaml` 은 위험 통제 4종(stop_loss=-1% / trailing=-0.5% / partial_take=+1.5% / daily_loss_kill) 활성, 모멘텀 게이트와 절대금액 캡은 비활성으로 시작:

- **위험 통제**: 종목별 stop-loss / 트레일링 스톱 / 부분 익절 (`PositionGuard`), 1회·일일 절대금액 한도, 일일 손실 도달 시 강제 청산(kill switch).
- **다회 진입**: 청산 후 쿨다운 경과 시 재진입 (`IndexMomentumDayTrade` 전략).
- **지수 일중 모멘텀 게이트**: KOSPI/KOSDAQ 직전 N분 수익률이 임계 이상일 때만 진입.
- **비용 가시화**: 거래세 0.20% / 슬리피지 5bp / 수수료 추정 → 일일 리포트에 "💸 비용 차감 PnL".
- **알림 throttle**: `--telegram-throttle 5` 로 매매 이벤트를 5분 윈도우 요약. 중요 이벤트(kill switch·시작·종료) 는 즉시 푸시.

샘플 프로파일: [strategies/profiles/daytrade-default.yaml](strategies/profiles/daytrade-default.yaml). 첫 활성은 한도 1~2개만 켜고 며칠 dry-run.

### 24단계 — 주간 뉴스 → 다음 주 day-trade 프로파일 자동 반영

매주 토요일 09:00 KST 의 launchd cron 이 다음 흐름을 실행:

1. 직전 7일 일일 리포트 + 한국경제·매일경제 RSS 뉴스 수집 (`data/news/news-*.json` 캐시)
2. LLM(Anthropic) 호출 1회 — 다음 주 프로파일 변경 제안 (`DaytradeProposal`)
3. 안전 캡(top_n ≤ 5, stop_loss ≤ 3% 등) 통과한 변경만 `strategies/profiles/daytrade-current.yaml` 에 적용. 기존은 `.bak.YYYY-MM-DD` 로 백업
4. 텔레그램에 "🗞 다음 주 day-trade 프로파일 갱신" 변경 요약 푸시

월요일 08:55 의 paper-day cron 이 자동으로 `daytrade-current.yaml` 을 읽어 그 주 운용에 반영. 파일 없으면 `daytrade-default.yaml` 로 폴백, 둘 다 없으면 CLI 인자만으로 동작 (안전).

수동 호출: `uv run auto-trade weekly-proposal --push` (또는 `--dry-run` 으로 입력 요약만).

LLM 비용은 호출당 약 $0.04~0.08 (Opus + 캐싱) → 주 1회 = 월 $0.16~0.32. [feedback_llm_cost](memory) 의 "주 1회 이하 저빈도" 규칙 안.

### 25단계 Phase A — KRX 휴장일 자동 skip

launchd 가 평일 08:55 만 보고 시동하지만 한국 법정공휴일·임시휴장(예: 어린이날·부처님오신날·대선)에도 그대로 켜져 텔레그램 노이즈가 발생. `paper`, `paper-day`, `weekly-proposal` 세 명령 모두 진입부에서 휴장일이면 안내 메시지 1회 + 정상 종료한다.

- 정적 테이블: `src/auto_trading/market/calendar.py` 의 `KRX_HOLIDAYS[2025|2026]` — 매년 12월 KRX 발표 직후 다음 해 항목을 추가한다(1줄 PR). 임시휴장도 같은 set 에 추가하면 즉시 반영.
- 휴장일 텔레그램 메시지 예: "📅 KRX 휴장일(2026-05-25) — paper-day 시동 생략 · 다음 거래일 2026-05-26".
- 우회: 수동 dry-run·디버그용으로 `--force` 옵션 모든 명령에 제공.
- weekly-proposal 은 토요일 자체는 항상 비거래일이라 별도 규칙 — 토요일 기준 다음 거래일까지 5일 이상이면(설/추석 등 긴 연휴) LLM 비용 절약을 위해 skip, 일반 토요일은 정상 진행.

### 25단계 Phase B — 로그 자동 정리

매일 02:30 KST launchd 가 `scripts/rotate-logs.sh` 실행:
- 30일↑ `*.log` → gzip (`.log.gz`)
- 90일↑ `.log.gz` → 삭제
- launchd 자체 로그 (`logs/launchd-*.log`) 가 10MB 넘으면 truncate (launchd 가 같은 fd 유지하므로 회전 X)

`scripts/launchd-install.sh` 에 자동 등록. 수동 검증: `LOGS_ROOT=/tmp/test zsh scripts/rotate-logs.sh`. 즉시 시동: `launchctl kickstart -k gui/$UID/com.mizeloble.auto-trade.log-rotate`.

---

## 12. 자주 막히는 곳 (트러블슈팅)

| 증상 | 원인/해결 |
|---|---|
| `8030` 에러 | 실거래 키로 모의투자 호출 시도. 키움 개발자센터에서 **모의투자용** 키 재발급 |
| `au10001` (rate limit) | 토큰 발급을 너무 자주 시도. 1분 정도 기다렸다 재시도 |
| `quote` 명령에서 응답 없음 | `.env` 의 키 오타, 또는 모의투자 신청 안 됨 |
| paper 가 곧바로 종료됨 | 장 외 시간(09:00 이전 / 15:30 이후) 또는 휴장일 |
| `paper-day` 가 매수 0 으로 종료 | 시장 약세장 (5/20일선 아래) 또는 후보 모두 ETF/SPAC 으로 필터됨. 정상 동작. |
| `--strategy` 와 `--profile` 둘 다 줬다고 에러 | 둘은 상호 배타. 한 쪽만 |
| YAML 수정 후 동작 이상 | 들여쓰기(빈칸) 가 깨짐. `auto-trade strategy suggest` 를 다시 돌려 새로 만드는 게 빠름 |
| 텔레그램 알림 안 옴 | `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` 둘 다 채워졌는지, chat.id 가 본인 것인지 확인 |
| 실거래 가고 싶지 않은데 갈까봐 무서움 | `.env` 의 `KIWOOM_MODE=mock` 만 유지하면 절대 실거래 안 갑니다 |

---

## 13. 보안 — 절대 깨지 말 것

- `.env` 는 **git에 커밋 금지** (`.gitignore` 에 이미 등록).
- AppKey/Secret/토큰을 **로그·스크린샷·메신저에 통째로 붙여넣지 않기**. 봇은 자동 마스킹하지만 사람이 실수할 수 있음.
- 키가 노출됐다 싶으면 즉시 키움 개발자센터에서 **재발급** → 옛 키 무효화.
- 실거래 전환은 **혼자 결정하지 말고**, 모의에서 충분히 (수 주 ~ 수 개월) 검증한 뒤에.

---

## 14. 폴더가 어떻게 생겼는지 (지도)

```
auto-stock-trading/
├── README.md              ← 빠른 개요
├── manual.md              ← (이 파일) 초심자용 자세한 매뉴얼
├── todo-list.md           ← 단계별 진행 체크리스트
├── .env.example           ← 설정 템플릿 (복사해서 .env 로)
├── pyproject.toml         ← Python 프로젝트 메타
├── strategies/profiles/   ← 추천 받아 저장된 YAML 들이 쌓이는 곳
├── data/backtests/        ← 백테스트 결과 (자동 생성)
├── logs/                  ← 운영 로그 (자동 생성)
├── scripts/               ← 자동 시작·종료 스크립트
└── src/auto_trading/      ← 봇의 본체 코드
    ├── cli.py             ← 명령어 진입점
    ├── runner.py          ← 실시간 매매 루프
    ├── config.py          ← .env 로드
    ├── kiwoom/            ← 키움 API 클라이언트
    ├── market/            ← 시세·거래량 순위·지수 일봉
    ├── account/           ← 잔고·주문
    ├── strategy/          ← 전략 + 추천 + Claude 조언
    ├── daytrade/          ← day-trade 발굴·게이트·universe 주입 오케스트레이터
    ├── risk/              ← 리스크 한도
    └── notify/            ← 텔레그램 알림·명령
```

---

## 15. 다음에 할 일 — 단계별 추천

1. **이번 주**: `quote` / `balance` 명령으로 인증 확인.
2. **다음 주**: `strategy suggest` → `backtest` 로 전략 두세 개 비교.
3. **그 다음 주**: 둘 중 하나 골라 dry-run 종일 운영:
   - swing: `paper --profile <yaml> --dry-run` — 본인이 정한 종목으로 며칠
   - day-trade: `paper-day --top-n 3 --dry-run` — 매일 자동 발굴, 그날 청산
4. **launchd 자동 운영 등록** — 평일 09:00 자동 시동 (§10).
5. **그 다음**: dry-run 만족스러우면 `--dry-run` 빼고 실 모의투자 매매.
6. **만족스러우면**: 텔레그램 연결 → 외출 중에도 모니터링·제어.
7. **수개월 검증 후**: 실거래 전환은 사용자가 직접 결정. (절대 봇이 임의로 바꾸지 않음.)

---

질문이 생기면 [README.md](README.md), [todo-list.md](todo-list.md), 그리고 각 폴더의 `CLAUDE.md` 를 먼저 확인하세요. 그래도 막히면 사람한테 물어보는 게 가장 빠릅니다.
