# auto-stock-trading

키움증권 REST API를 이용한 Python 자동 주식 매매 봇. **모의투자(가상 돈) 기반으로 검증한 뒤에만 실거래로 넘어간다.**

운영 모드는 **day-trade(`paper-day`) 가 메인** — 매일 09:00 거래량 상위 종목을 자동 발굴해 그날 안에 청산하고, 토요일 LLM cron 이 다음 주 프로파일을 자동 갱신한다. 미리 정한 종목으로 며칠 보유하는 swing(`paper`) 모드는 학습·단일 종목 검증용으로 남겨 둔다. 코드 한 줄 안 짜도 새 전략을 쓸 수 있는 게 목표.

> **이 README 는 빠른 개요다. 자세한 설명은 정본 문서 [manual.md](./manual.md) 를 본다** (초심자용, 단계별). README 와 manual 이 어긋나면 manual 이 맞다.

## 빠른 시작

```bash
uv sync                  # .venv 생성 + 의존성 설치 (Python 3.14)
cp .env.example .env     # KIWOOM_APP_KEY / KIWOOM_APP_SECRET 에 모의투자 키 입력
uv run auto-trade quote 005930        # 인증 확인 (현재가 표 출력되면 성공)
uv run auto-trade paper-day --top-n 3 --dry-run   # 첫 시운전 (주문 전송 X)
```

> 모의투자 AppKey 는 [키움 개발자 센터](https://openapi.kiwoom.com) 에서 모의투자 신청 **후** 발급(실거래 키와 별개). 설치·`.env` 항목 전체는 [manual.md §1–2](./manual.md).

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
uv run auto-trade weekly-proposal --push       # (LLM) 다음 주 프로파일 자동 갱신
uv run auto-trade dashboard                    # 결과 분석 웹 대시보드 (127.0.0.1:8000)

# 운영 진단
uv run auto-trade preflight --profile <yaml>   # 실거래 전환 전 안전 게이트 점검
uv run auto-trade ops                          # launchd 자동 잡(8종) 상태 한눈에
uv run auto-trade detect-regime                # 시장국면 진단
uv run auto-trade orders inspect               # 주문 저널 진단
uv run auto-trade heartbeat-check              # 러너 생존(heartbeat) 점검
uv run auto-trade latency --days 7             # 신호→주문 지연 통계
uv run auto-trade vi                           # VI(변동성완화장치) 발동종목 1회 스냅샷 (read-only)

# 시총 200 breadth 인프라 (평일 launchd 자동 — 수동 호출은 디버그용)
uv run auto-trade build-marketcap-universe     # 시총 상위 200 마스터 갱신 (08:50)
uv run auto-trade snapshot-top200              # 시총 200 + 당일 발굴 종목 스냅샷 1라운드 (09:00~15:30 10분)

# swing (선택 · 학습/검증용)
uv run auto-trade strategy suggest …           # 추천 → YAML 프로파일 저장
uv run auto-trade backtest …                   # 과거 일봉 시뮬
uv run auto-trade paper …                      # 단일 종목 실시간 루프
uv run auto-trade strategy advise …            # (LLM) 프로파일 조정
```

## 자세한 설명 — manual.md 로

| 주제 | manual.md 절 |
|---|---|
| 시스템 개요 / 사전 준비 / 설치 | [§0–2](./manual.md) |
| 명령 한눈에 보기 | [§3](./manual.md) |
| **day-trade dry-run 따라하기 (메인)** | [§4](./manual.md) |
| 일일 리포트 + 주간 자동 갱신 (LLM) | [§5](./manual.md) |
| 내장 전략 (sma_crossover / rsi_reversion / intraday_momentum / index_momentum_daytrade) | [§6](./manual.md) |
| 안전장치 (리스크 매니저) | [§7](./manual.md) |
| 모드 비교 (mock/real, swing/day-trade) | [§8](./manual.md) |
| (선택) Swing 흐름 (단일 종목 학습·검증) | [§9](./manual.md) |
| 텔레그램 알림 + 원격 명령 | [§10](./manual.md) |
| Claude 전략 조언 | [§11](./manual.md) |
| 매일 자동 켜고 끄기 (launchd) | [§12](./manual.md), [scripts/launchd/README.md](./scripts/launchd/README.md) |
| 운영 진단·점검 명령 (preflight 등) | [§13](./manual.md) |
| 트러블슈팅 / 보안 / 폴더 지도 | [§14–16](./manual.md) |

## 안전장치 (요약)

- 기본 `KIWOOM_MODE=mock`. 실거래 전환은 `KIWOOM_MODE=real` + `--allow-real` 둘 다 + 사용자 명시 승인 후에만.
- 모든 주문 경로에 리스크 한도 강제(종목 20% / 포트폴리오 80% / 일일 손실 3% + stop/트레일링/부분익절/kill switch). 회전율 가드로 종목당 일일 매수 횟수도 제한 가능.
- 거래불가 종목 자동 회피 — 모의투자 매매제한·거래정지 등으로 한 번 거부당한 종목은 그날 즉시 재시도 차단 + 다음 발굴부터 제외(`data/state/untradeable.json`, 30일 노화). 발굴은 후보를 넉넉히 받아 차단분을 다음 순위로 메움(슬롯 낭비 방지).
- (선택·기본 OFF) 시장국면 자동 라우팅(KODEX 200) · 시총 200 breadth 게이트 · 회피 종목 목록 — 약세장·과매매에서 매수 자체를 줄이는 추가 게이트(§7). 라우팅이 켜져 있으면 그날 실제 적용본은 레짐 프로파일이며, 회피 종목은 원본 프로파일 목록과 자동 합산 적용.
- LLM 호출은 사람 명시 트리거 또는 합의된 주 1회 cron 만 (월 상한 $5). 매일·매시간 금지.
- `paper-day` / `paper` 모두 `--dry-run` 기본. 시크릿은 git 제외 + 로그 마스킹.

> 한도·게이트·LLM 안전 캡의 정확한 값과 동작은 [manual.md §7](./manual.md) 가 정본.

## 테스트

```bash
uv run pytest                       # 단위 테스트 (네트워크 X)
uv run pytest -m integration        # 모의투자 실 API 호출 (.env 필요)
uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy
```

## 진행 상황

진행 중 작업·남은 과제는 [todo-list.md](./todo-list.md), 완료된 단계별 이력은 [docs/history.md](./docs/history.md) 참고. 전략 파라미터 변경은 반드시 반사실 검증 후에만 한다 — 검증 스크립트·결론은 [scripts/diagnostics/](./scripts/diagnostics/) (2026-07-04 시리즈: 쿨다운·진입시각·출구 폭 전 축 "현행 유지" 판정).
