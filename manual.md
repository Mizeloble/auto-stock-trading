# 초심자 매뉴얼 — auto-stock-trading

> 코딩도, 주식도 처음이라도 따라올 수 있게 만든 매뉴얼입니다.
> 모든 명령은 **터미널(맥은 Terminal, 윈도우는 PowerShell)** 에 그대로 복사·붙여넣기 하면 됩니다.
> 절대 실수해서는 안 되는 단 한 가지: **`KIWOOM_MODE=mock`(모의투자) 로 시작하기**. 진짜 돈은 충분히 검증한 뒤에만 씁니다.
>
> [README.md](README.md) 는 빠른 개요이고, **이 문서가 정본** 입니다. 둘이 어긋나면 이 문서가 맞습니다.

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

**메인 운영 모드는 day-trade(`paper-day`)** — 매일 아침 거래량 상위 종목을 자동 발굴해 그날 안에 청산합니다(§4). 미리 정한 종목을 며칠 보유하는 swing(`paper`) 은 학습·검증용 선택 모드입니다(§9).

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

## 3. 명령 한눈에 보기

day-trade(`paper-day`) 가 메인 운영, swing(`paper`) 은 학습·단일 종목 검증용입니다. 운영 중 상태를 들여다보는 진단 명령은 §13 에 따로 모았습니다.

| 명령 | 무엇을 하나 |
|---|---|
| `uv run auto-trade --help` | 전체 명령어 목록 |
| `uv run auto-trade quote 005930` | 한 종목 현재가 보기 |
| `uv run auto-trade balance` | 모의 계좌 잔고/평가금액 |
| **day-trade (메인)** | |
| `uv run auto-trade paper-day --top-n 3 --dry-run` | 거래량 상위 3종목 자동 선정 → 즉시 매수 → 15:15 일괄 청산. §4. |
| `uv run auto-trade paper-day --profile strategies/profiles/daytrade-default.yaml --dry-run` | 정통 데이트레이딩 풀 패키지 (PositionGuard / 다회 진입 / kill switch / 비용 차감 PnL). §4·§7. |
| `uv run auto-trade report --date YYYY-MM-DD --push` | 그날 paper-day 로그 → 일일 리포트 JSON + 텔레그램 푸시. §5. |
| `uv run auto-trade insight --days 7 --push` | (선택·LLM) 직전 7일 리포트 누적 분석 → 한국어 인사이트. §5. |
| `uv run auto-trade weekly-proposal --dry-run` | (선택·LLM) 다음 주 프로파일 자동 갱신 시뮬. 토요일 cron 자동 호출. §5. |
| **swing (선택)** | |
| `uv run auto-trade strategy suggest --symbol 005930` | 그 종목에 어울리는 전략 추천 + YAML 저장. §9. |
| `uv run auto-trade backtest --profile <yaml> --from 2024-01-01` | 과거 데이터 시뮬레이션. §9. |
| `uv run auto-trade paper --profile <yaml> --dry-run` | 단일 종목 실시간 시운전 (주문 미전송). §9. |
| `uv run auto-trade strategy advise --profile <yaml>` | (선택·LLM) Claude 가 YAML 숫자 조언. §11. |

> 윈도우 PowerShell 에서 줄을 `\` 로 이어 적을 땐 `` ` `` 백틱을 쓰세요.

---

## 4. 가장 먼저: day-trade dry-run 따라하기 (메인)

**무엇** — 종목을 미리 정해 들고 가는 swing 과 달리, 매일 아침 거래량 상위 종목을 자동으로 골라 그날 안에 청산하는 모드. 이게 이 봇의 메인 운영 방식입니다.

**왜** — 다음날 갭 리스크(밤사이 호재/악재로 가격이 갑자기 튀거나 빠지는 위험) 를 0 으로 만들고, 시장 변동성을 그날그날 따라가기 위해.

### 하루 흐름

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

- `--top-n 3` : 후보 종목 수 (3종목 권장 — 리스크 매니저 캡과 잘 맞고 모니터링 부담 적음).
- `--dry-run` : 실제 주문 안 보냄. 시그널/리스크 로직만 검증. **처음엔 반드시 이걸로**.
- `--liquidate 15:15` : 청산 시각 (기본 15:15 KST. 정규장 마감 15:30 직전).
- `--include-etf` : (선택) ETF/ETN/SPAC 도 매수 대상에 포함. 기본은 제외.
- `--discover-delay 30` : 09:00 직후 거래량 데이터 안정화 대기 초. 빠른 검증 시 0.
- `--profile <yaml>` : DaytradeProfile YAML 로드. 지정 시 strategy/top_n/liquidate/리스크 한도가 프로파일 값으로 덮어써짐. 샘플: [strategies/profiles/daytrade-default.yaml](strategies/profiles/daytrade-default.yaml).
- `--telegram-throttle 5` : 매매 알림 5분 윈도우 요약. 다회 진입 시 채터링 방지. 0=비활성.
- `--force` : 휴장일 안전 차단(§12)을 무시하고 강제 시동. 수동 dry-run·디버그용.

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

### 텔레그램 알림 (설정돼 있으면 — §10)

- `🔎 발굴 완료 · 매수 후보 N건` (선정/차단/메모)
- `✅ 체결 접수` (매수 N건)
- `🧹 청산 완료 (dry-run) · N종목`
- `⏹ paper 정지 (정상 종료)`

`/status` 보내면 `▶ 운영중 · day-trade (청산 15:15)` 처럼 모드 표시.

### 매수가 안 들어가는 경우 (정상일 수 있음)

- **모든 후보가 게이트 차단**: 약세장(시장 5/20일선 아래) 이라 보호 차원. **매수 0** + 즉시 종료. 정상.
- **발굴 결과 0건**: 키움 응답 이상 — 로그 `daytrade.discovery_failed` 확인.
- 예: KOSPI/KOSDAQ 둘 다 5일선 아래면 후보 전부 차단 → 매수 없이 종료.

### 정통 데이트레이딩 풀 패키지 — `--profile`

`--top-n` 만 쓰면 균등 매수 + 15:15 청산의 단순 흐름입니다. `--profile strategies/profiles/daytrade-default.yaml` 을 주면 다음이 한 번에 켜집니다:

- **위험 통제**: 종목별 stop-loss / 트레일링 스톱 / 부분 익절 (`PositionGuard`), 일일 손실 도달 시 강제 청산(kill switch). (한도 표 → §7)
- **다회 진입**: 청산 후 쿨다운 경과 시 재진입 (`index_momentum_daytrade` 전략 — §6).
- **지수 일중 모멘텀 게이트**: KOSPI/KOSDAQ 직전 N분 수익률이 임계 이상일 때만 진입. (기본 프로파일에서는 OFF — 첫 운용 안정성 위해)
- **비용 가시화**: 거래세 0.20% / 슬리피지 5bp / 수수료 추정 → 일일 리포트에 "💸 비용 차감 PnL".

기본 `daytrade-default.yaml` 의 실제 프리셋: stop_loss −1% / trailing −0.5% / partial_take +1.5%(절반) / daily_loss_kill ON, 모멘텀 게이트·절대금액 캡은 OFF. 첫 활성은 이대로 며칠 dry-run 후, 한도를 한두 개씩 손대세요.

---

## 5. 일일 리포트 + 주간 자동 갱신 (LLM)

`paper-day` 가 종료될 때마다 자동으로 일일 리포트가 `data/reports/daily-YYYY-MM-DD.json` 으로 저장되고, 텔레그램이 설정돼 있으면 푸시됩니다(페어 PnL, 마감 잔고, 비용 차감 PnL).

매주 토요일 launchd cron 이 두 번에 나눠 LLM 작업을 자동 실행합니다:

| 시점 (KST) | 동작 |
|---|---|
| 토요일 06:00 | `auto-trade fetch-news` — RSS 6개 소스를 미리 받아 캐시에 누적 (LLM 호출 X · 비용 0) |
| 토요일 09:00 | `auto-trade insight` (사람용 인사이트) → `auto-trade weekly-proposal` (다음 주 프로파일 자동 갱신) |

`weekly-proposal` 흐름:

1. 직전 7일 일일 리포트 + 6개 RSS 뉴스(한국경제 finance·economy, 연합뉴스 economy·market, SBS Biz, 파이낸셜뉴스) 수집 (`data/news/news-*.json` 캐시, url 키 dedupe 후 누적 병합)
2. LLM(Anthropic) 호출 **1회** — 다음 주 프로파일 변경 제안 (`DaytradeProposal`)
3. 안전 캡(top_n ≤ 5, stop_loss ≤ 3% 등) 통과한 변경만 `strategies/profiles/daytrade-current.yaml` 에 적용. 기존은 `.bak.YYYY-MM-DD` 백업
4. 텔레그램에 "🗞 다음 주 day-trade 프로파일 갱신" 변경 요약 푸시

월요일 08:55 의 paper-day cron 이 자동으로 `daytrade-current.yaml` 을 읽어 그 주 운용에 반영. 파일 없으면 `daytrade-default.yaml` 폴백, 둘 다 없으면 CLI 인자만으로 동작(안전).

수동 호출 / 디버그:
```bash
uv run auto-trade fetch-news --days 7              # RSS 캐시만 갱신 (LLM X)
uv run auto-trade weekly-proposal --dry-run        # LLM 호출 없이 입력 요약만
uv run auto-trade weekly-proposal --push           # 실제 LLM 호출 + 프로파일 갱신
```

**LLM 비용**: 호출당 약 $0.4~0.5 (Opus + 캐싱), 주 1회 cron = **월 약 $2** (사용자 합의 상한 $5 이내). LLM 호출은 사람이 명령을 칠 때 또는 합의된 주 1회 cron 만 — 매일·매시간 호출은 하지 않습니다.

---

## 6. 내장 전략

전략은 "지금 사라/팔라/가만히" 라는 `Signal` 만 반환합니다 — 실제 주문은 다음 단계의 **리스크 매니저(§7)** 가 결정합니다.

### swing 용 전략

**6-1. SMA 골든크로스 (`sma_crossover`)**
- **무엇** — 단기 이동평균(예: 5일)이 장기 이동평균(예: 20일) 을 위로 뚫으면 매수, 아래로 뚫으면 매도.
- **언제 잘 통하나** — 한 방향으로 추세가 분명할 때 (상승/하락 추세장).
- **파라미터** — `short` (기본 5), `long` (기본 20).

**6-2. RSI 역추세 (`rsi_reversion`)**
- **무엇** — RSI 가 30 미만이면 "너무 팔렸다" → 매수, 70 초과면 "너무 샀다" → 매도.
- **언제 잘 통하나** — 박스권(가격이 위아래로 왔다갔다) 일 때.
- **파라미터** — `period` (기본 14), `oversold` (기본 30), `overbought` (기본 70).

### day-trade 용 전략 (둘은 다른 전략입니다)

**6-3. 첫 봉 1회 진입 (`intraday_momentum`)**
- **무엇** — 발굴된 종목의 첫 봉이 들어오는 순간 BUY 시그널을 **한 번만** 발사. 이후 침묵.
- **언제** — 단순 균등 매수만 원할 때. 직접 호출은 비권장(day-trade 모드 전용).
- **파라미터** — `weight` (기본 1.0). orchestrator 가 자동으로 1/N 분할 주입.

**6-4. 다회 진입 + 모멘텀 게이트 (`index_momentum_daytrade`) — 기본 프로파일이 쓰는 전략**
- **무엇** — 보유 중 아니고 + 청산 후 쿨다운(`cooldown_min`) 경과 시 재진입. (선택) 지수 일중 모멘텀 게이트가 켜져 있으면 KOSPI/KOSDAQ 직전 N분 수익률이 임계 이상일 때만 진입.
- **왜 이게 default 인가** — 하루 한 번이 아니라 청산 후 조건이 다시 맞으면 또 들어가, 변동성 있는 날 기회를 더 잡기 위해. `daytrade-default.yaml` 의 `strategy: index_momentum_daytrade`.
- **파라미터** — `weight` (기본 1.0), `cooldown_min` (기본 30), `momentum_lookback_min` (기본 5), `momentum_threshold_pct` (기본 0.0 = 게이트 OFF). 청산 신호는 이 전략이 만들지 않고 PositionGuard(stop/trail/partial) 또는 15:15 일괄 청산이 처리.

---

## 7. 안전장치 (리스크 매니저)

모든 주문은 리스크 매니저(`src/auto_trading/risk/manager.py`)를 통과합니다. 한도에 걸리면 로그에 `risk_reject reason=...` 이 남습니다. **한도 완화는 사용자가 명시 승인할 때만.**

### 항상 켜져 있는 기본 한도 (보수적 기본값)

| 한도 | 기본값 | 효과 |
|---|---|---|
| 종목당 최대 비중 | 예수금의 **20%** | 한 종목에 몰빵 방지 |
| 포트폴리오 총 익스포저 | 예수금의 **80%** | 항상 일부 현금 보유 |
| 일일 손실 한도 | -**3%** | 그날 -3% 도달 시 신규 매수 정지 |
| 최소 주문 금액 | **10,000원** | 너무 작은 주문(수수료 손해) 차단 |

> 초보가 가장 자주 잃는 패턴은 "한 종목에 다 넣고 손실 누적". 위 한도가 자동으로 끊어 줍니다.

### 프로파일로 켜는 추가 한도 (기본 OFF — 의식적으로 활성)

day-trade `--profile` YAML 의 `risk:` 블록으로 켭니다. 기본값은 모두 비활성(0/False)이고, `daytrade-default.yaml` 이 일부를 켜 둡니다. 토요일 weekly-proposal LLM 이 안전 캡 안에서 점진 조정합니다.

| 한도 | 기본 | daytrade-default | 효과 |
|---|---|---|---|
| `stop_loss_pct` | 0 (OFF) | 0.01 (−1%) | 매수가 대비 −X% 도달 시 전량 손절 |
| `trailing_stop_pct` | 0 (OFF) | 0.005 (−0.5%) | 보유 중 고점 대비 −X% 도달 시 잔량 청산 |
| `partial_take_pct` | 0 (OFF) | 0.015 (+1.5%) | 매수가 대비 +X% 도달 시 부분 익절 |
| `partial_take_qty_ratio` | 0.5 | 0.5 | 부분 익절 비율 (0.5 = 절반만) |
| `daily_loss_kill` | False | **True** | 일일 −3% 도달 시 보유 강제 청산 + 정지 |
| `max_order_value` | 0 (무제한) | 0 | 1회 주문 절대 금액 캡(원) |
| `daily_notional_limit` | 0 (무제한) | 0 | 일일 총 매수대금 캡(원) |
| `max_consecutive_losses` | 0 (OFF) | — | N연속 손절 시 일정 시간 신규 진입 차단 |
| `consecutive_loss_pause_sec` | 86,400 (24h) | — | 위 차단 지속 시간 |
| `max_position_correlation` | 0 (OFF) | — | 보유 종목 간 상관계수 가드 (권장 0.7) |
| `atr_normalized_weight` | False | — | 변동성 기반 weight 정규화 |
| `atr_target_risk_pct` | 0.01 | — | 위 정규화 기준 ("1 ATR ≈ 자본 1%") |

> 첫 활성은 한두 개만 켜고 며칠 dry-run. 한 번에 다 켜지 마세요.

---

## 8. 모드 비교 — 절대 헷갈리면 안 됨

### mock vs real (가상 돈 vs 진짜 돈)

| 항목 | `KIWOOM_MODE=mock` | `KIWOOM_MODE=real` |
|---|---|---|
| 돈 | 가상 (모의투자) | **진짜** |
| 키 | 모의투자 AppKey | 실거래 AppKey (다른 키!) |
| 명령 | 그냥 동작 | 추가로 `--allow-real` 플래그 필요 |
| 권장 단계 | 항상 여기서 시작 | 모의에서 수개월 검증 후 |

`real` 로 바꾸려면 **(a)** `.env` 의 `KIWOOM_MODE=real` 수정 **+ (b)** 명령에 `--allow-real` 직접 입력. 둘이 동시에 충족되지 않으면 봇이 자동 거부합니다. 실거래 전환 전 반드시 `auto-trade preflight` 로 안전 게이트를 통과시키세요(§13).

### swing vs day-trade (운영 방식)

| 항목 | swing (`paper`) — 학습용 | day-trade (`paper-day`) — **메인** |
|---|---|---|
| 종목 결정 | YAML 에 미리 명시 | 매일 09:00 자동 발굴 |
| 매수 시점 | 전략 신호 발생 시 | 발굴 직후 즉시 균등 매수 |
| 청산 | 외부 stop 시각 (다음날도 보유 가능) | 15:15 자동 일괄 매도 |
| 다음날 갭 리스크 | 있음 | **없음** (당일 청산) |
| 자동화 | launchd 미등록 (수동 실행) | launchd 평일 08:55 / 토요일 LLM cron |
| LLM 갱신 | `strategy advise` 사람 트리거 | `weekly-proposal` 토요일 cron 자동 |

> 정기 운영은 day-trade 만 권장. swing 은 새 종목·새 전략 감 잡을 때 dry-run 으로만.

---

## 9. (선택) Swing 흐름 — 단일 종목 학습·검증

day-trade 가 메인이지만, **새 전략을 감 잡거나 특정 종목을 며칠 보유하며 시험**하고 싶을 때 swing 흐름이 짧고 직관적입니다. 정기 운영(launchd) 에서는 빠진 모드라 수동으로만 실행합니다.

```
①  추천 받기   → ②  YAML 숫자 조정 (선택)   → ③  백테스트   → ④  paper --dry-run   → ⑤  paper 실전(모의)
```

```bash
# ① 시장국면 분석 → 어울리는 전략·파라미터 추천 → strategies/profiles/auto_005930.yaml
uv run auto-trade strategy suggest --symbol 005930

# ③ 과거 일봉 백테스트 (수익률·승률·MDD 표 + data/backtests 저장)
uv run auto-trade backtest --profile strategies/profiles/auto_005930.yaml --from 2024-01-01

# ④ 실시간 시세 + 전략·리스크 로직 검증 (주문 미전송, [DRY] 로그만)
uv run auto-trade paper --profile strategies/profiles/auto_005930.yaml --dry-run

# ⑤ 모의투자 계좌에 실제 매수·매도 (가상 돈). Ctrl+C 로 중단, 15:30 자동 종료
uv run auto-trade paper --profile strategies/profiles/auto_005930.yaml
```

② YAML 숫자 조정은 메모장에서 `period: 14` → `12` 같이 숫자만 바꾸세요. **들여쓰기(빈칸)는 절대 건드리지 말 것** — YAML 은 들여쓰기로 의미를 구분합니다. 망가지면 `strategy suggest` 를 다시 돌려 새로 만드는 게 빠릅니다. 백테스트 결과 수익률 마이너스거나 MDD > 20% 면 다른 전략을 시도하세요.

생성되는 YAML 형태:
```yaml
name: auto_005930
symbol: "005930"
strategy: rsi_reversion        # sma_crossover 또는 rsi_reversion
params:
  period: 14
  oversold: 30
  overbought: 70
weight: 1.0
```

`strategy suggest` 의 시장국면 분류 (`src/auto_trading/strategy/scanner.py`):

| 국면 | 판정 | 추천 전략 |
|---|---|---|
| `trend_up` | 일평균 기울기 ≥ +0.3% | SMA 5/20 골든크로스 |
| `trend_down` | 일평균 기울기 ≤ −0.3% | SMA 5/20 (보수적 weight) |
| `range` | 기울기 약함, 밴드폭 < 8% | RSI(14) 30/70 역추세 |
| `high_vol` | ATR ≥ 3% / 봉 | RSI(7) 25/75 역추세 |

추천을 거치지 않고 직접 호출하려면 `--strategy` + `--symbol(s)` (프로파일과 상호 배타).

---

## 10. (선택) 텔레그램 알림 + 원격 명령

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

## 11. (선택) Claude 로 전략 조언 받기

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

## 12. (선택) 매일 자동으로 켜고 끄기 — macOS launchd

회사에 있을 때도 **08:55 자동 시작 / 15:15 자동 청산 → 종료** 로 돌릴 수 있습니다. 권장 모드는 day-trade(§4).

```bash
./scripts/launchd-install.sh
launchctl list | grep mizeloble.auto-trade
```

`launchd-install.sh` 한 줄로 다음 **5개 잡** 이 함께 등록됩니다:

| 잡 | 시점 (KST) | 역할 |
|---|---|---|
| `paper-day` | 평일 08:55 | day-trade 시동 (`daytrade-current.yaml` → 없으면 `daytrade-default.yaml`) |
| `news-prefetch` | 토요일 06:00 | RSS 캐시 선반영 (LLM X) |
| `insight-weekly` | 토요일 09:00 | insight → weekly-proposal LLM cron |
| `log-rotate` | 매일 02:30 | 로그 자동 정리 (아래) |
| `heartbeat-check` | 평일 09:00~16:00, 5분 간격 | 봇 생존 감시 (heartbeat 누락 시 텔레그램 알림) |

해제·상세는 [scripts/launchd/README.md](scripts/launchd/README.md) 참고.

> swing 모드 자동화 plist 는 deprecated 처리되어 `scripts/legacy/` 로 이동했습니다. 복귀 절차는 [scripts/legacy/README.md](scripts/legacy/README.md).

### 휴장일 자동 skip

launchd 는 평일 08:55 만 보고 시동하지만, 한국 법정공휴일·임시휴장(어린이날·부처님오신날·대선 등)에도 켜져 텔레그램 노이즈가 생깁니다. `paper`, `paper-day`, `weekly-proposal` 은 진입부에서 휴장일이면 안내 메시지 1회 + 정상 종료합니다.

- 정적 테이블: `src/auto_trading/market/calendar.py` 의 `KRX_HOLIDAYS` — 매년 12월 KRX 발표 직후 다음 해 항목 추가(1줄 PR). 임시휴장도 같은 set 에 추가하면 즉시 반영.
- 메시지 예: "📅 KRX 휴장일(2026-05-25) — paper-day 시동 생략 · 다음 거래일 2026-05-26".
- 우회: 수동 dry-run·디버그용으로 `--force` 옵션 제공.

### 로그 자동 정리

매일 02:30 KST `scripts/rotate-logs.sh` 실행:
- 30일↑ `*.log` → gzip (`.log.gz`)
- 90일↑ `.log.gz` → 삭제
- launchd 자체 로그(`logs/launchd-*.log`)가 10MB 넘으면 truncate

수동 검증: `LOGS_ROOT=/tmp/test zsh scripts/rotate-logs.sh`. 즉시 시동: `launchctl kickstart -k gui/$UID/com.mizeloble.auto-trade.log-rotate`.

---

## 13. 운영 진단·점검 명령

운영을 시작한 뒤 "지금 상태가 정상인가?" 를 확인하는 명령들입니다. 평소엔 안 써도 되고, 문제가 의심되거나 실거래 전환을 검토할 때 씁니다.

> **`preflight` — 실거래 전환 전 필수 게이트**
> ```bash
> uv run auto-trade preflight --profile strategies/profiles/daytrade-current.yaml
> ```
> 리스크 한도 활성 / 미해결 주문 0건 / heartbeat 최신 / 최근 손익 MDD / 최근 백테스트 Sharpe 등을 자동 점검해 표로 출력합니다. **모두 통과(exit 0)해야 `--allow-real` 로 넘어가세요(§8).** 하나라도 실패면 exit 1.

| 명령 | 무엇 | 언제 쓰나 |
|---|---|---|
| `auto-trade dashboard` | 투자 결과 분석 웹 (FastAPI, 로컬 전용). `--host/--port` 옵션 | 며칠 운영 후 자산곡선·일별 상세를 브라우저로 보고 싶을 때 |
| `auto-trade detect-regime` | 현재 시장국면(추세상승/추세하락/박스/고변동성) 분류 | 어떤 day-trade 프로파일이 어울릴지 감 잡을 때 |
| `auto-trade heartbeat-check` | 봇 heartbeat 누락 검사 (cron 5분 간격 가정) | launchd 가 자동 호출. 수동으로 생존 확인할 때 |
| `auto-trade latency` | 주문 송신 지연 일별 통계 (p50/p95/p99) | 주문이 느리게 나가는 게 의심될 때 |
| `auto-trade orders inspect` | 주문 저널 진단·복구 (`--suspect-only`, `--tail`) | preflight 의 미해결 주문 게이트가 실패했을 때 무엇이 미해결인지 확인 |
| `auto-trade data check` | 데이터 파일 진단·정합성 검사 | 리포트/캐시 파일이 깨졌나 의심될 때 |
| `auto-trade verify-symbol` | 키움 일봉 fetch + OHLCV 정합성 검사 | 특정 종목 데이터가 이상할 때 |
| `auto-trade fetch-news` | RSS 뉴스만 미리 fetch → 캐시 갱신 (LLM X · 비용 0) | weekly-proposal 입력 미리 채워두기 (§5) |
| `auto-trade backtest-compare` | 두 백테스트 결과 비교 (Sharpe/PSR/CAGR/MDD/승률 차이 표) | 전략·파라미터 변경 전후 성능 비교 |

각 명령의 옵션은 `uv run auto-trade <명령> --help` 로 확인하세요. `data` / `orders` 는 하위 명령이라 `uv run auto-trade data --help` 처럼 봅니다.

---

## 14. 자주 막히는 곳 (트러블슈팅)

| 증상 | 원인/해결 |
|---|---|
| `8030` 에러 | 실거래 키로 모의투자 호출. 키움 개발자센터에서 **모의투자용** 키 재발급 |
| `au10001` (rate limit) | 토큰 발급을 너무 자주 시도. 1분 정도 기다렸다 재시도 |
| `quote` 명령 응답 없음 | `.env` 의 키 오타, 또는 모의투자 신청 안 됨 |
| paper 가 곧바로 종료됨 | 장 외 시간(09:00 이전 / 15:30 이후) 또는 휴장일(§12). 강제 시동은 `--force` |
| `paper-day` 가 매수 0 으로 종료 | 시장 약세장(5/20일선 아래) 또는 후보 모두 ETF/SPAC 필터. 정상 동작(§4) |
| `--strategy` 와 `--profile` 둘 다 줬다고 에러 | 둘은 상호 배타. 한 쪽만 |
| YAML 수정 후 동작 이상 | 들여쓰기(빈칸)가 깨짐. `strategy suggest` 다시 돌려 새로 만드는 게 빠름 |
| 텔레그램 알림 안 옴 | `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` 둘 다 채워졌는지, chat.id 가 본인 것인지 확인 |
| `preflight` 가 미해결 주문으로 실패 | `auto-trade orders inspect --suspect-only` 로 무엇이 미해결인지 확인(§13) |
| heartbeat 알림이 계속 옴 | 봇이 행 걸렸거나 정지. launchd 상태·로그 확인 |
| 실거래 갈까봐 무서움 | `.env` 의 `KIWOOM_MODE=mock` 만 유지하면 절대 실거래 안 감 |

---

## 15. 보안 — 절대 깨지 말 것

- `.env` 는 **git에 커밋 금지** (`.gitignore` 에 이미 등록).
- AppKey/Secret/토큰을 **로그·스크린샷·메신저에 통째로 붙여넣지 않기**. 봇은 자동 마스킹하지만 사람이 실수할 수 있음.
- 키가 노출됐다 싶으면 즉시 키움 개발자센터에서 **재발급** → 옛 키 무효화.
- 실거래 전환은 **혼자 결정하지 말고**, 모의에서 충분히(수 주 ~ 수 개월) 검증 + `preflight` 통과 후에.

---

## 16. 폴더가 어떻게 생겼는지 (지도)

```
auto-stock-trading/
├── README.md              ← 빠른 개요 (이 문서가 정본)
├── manual.md              ← (이 파일) 초심자용 자세한 매뉴얼
├── todo-list.md           ← 진행 중 작업 + 남은 과제
├── docs/history.md        ← 완료된 단계 이력 (PR 번호·날짜)
├── .env.example           ← 설정 템플릿 (복사해서 .env 로)
├── pyproject.toml         ← Python 프로젝트 메타
├── strategies/profiles/   ← 추천·LLM 갱신으로 쌓이는 YAML 들
├── data/                  ← 리포트·뉴스·백테스트·저널 (자동 생성)
├── logs/                  ← 운영 로그 (자동 생성)
├── scripts/               ← launchd 자동 시작·종료·로그정리 스크립트
└── src/auto_trading/      ← 봇의 본체 코드
    ├── cli/               ← 명령어 진입점 (_trading/_reporting/_backtest/_ops/_strategy)
    ├── runner/            ← 실시간 매매 루프 (+ aggregator·control)
    ├── config.py          ← .env 로드
    ├── _common.py         ← 공통 타입(Bar/Side 등)
    ├── heartbeat.py       ← 봇 생존 신호 (dead-man switch)
    ├── preflight.py       ← 실거래 전환 전 안전 게이트
    ├── kiwoom/            ← 키움 API 클라이언트
    ├── market/            ← 시세·거래량 순위·지수 일봉·휴장일
    ├── account/           ← 잔고·주문·저널·복구
    ├── strategy/          ← 전략 + 추천 + Claude 조언
    ├── daytrade/          ← day-trade 발굴·게이트·universe 오케스트레이터
    ├── backtest/          ← 백테스트 엔진
    ├── reports/           ← 일일·주간 리포트
    ├── llm/               ← Claude API 호출 래퍼·비용 추적
    ├── news/              ← 뉴스 수집·LLM 프로파일 갱신
    ├── shadow/            ← 섀도우(미체결 가정) 추적
    ├── monitor/           ← 헬스·지연 모니터
    ├── dashboard/         ← 결과 분석 웹 (FastAPI + Jinja2)
    ├── risk/              ← 리스크 한도
    └── notify/            ← 텔레그램 알림·명령
```

---

## 17. 다음에 할 일 — 순서대로

1. **이번 주**: `quote` / `balance` 로 인증 확인(§2·§3).
2. **다음 주**: `paper-day --top-n 3 --dry-run` 으로 day-trade 종일 dry-run(§4). 발굴/게이트/청산 로그 흐름 익히기.
3. **그 다음 주**: `paper-day --profile strategies/profiles/daytrade-default.yaml --dry-run` — 위험 통제 풀 패키지로 며칠 dry-run(§4·§7).
4. **launchd 자동 운영 등록**(§12) — 평일 08:55 자동 시동 + 토요일 LLM cron.
5. **dry-run 만족스러우면**: `--dry-run` 빼고 실 모의투자 매매. 텔레그램(§10) 으로 외출 중에도 모니터링·제어.
6. **(선택) 새 종목·새 전략 감 잡고 싶을 때**: §9 swing 흐름으로 단일 종목 검증.
7. **수개월 검증 + `preflight` 통과 후**(§13): 실거래 전환은 사용자가 직접 결정 (절대 봇이 임의로 바꾸지 않음).

---

질문이 생기면 [README.md](README.md), [docs/history.md](docs/history.md), 그리고 각 폴더의 `CLAUDE.md` 를 먼저 확인하세요. 그래도 막히면 사람한테 물어보는 게 가장 빠릅니다.
