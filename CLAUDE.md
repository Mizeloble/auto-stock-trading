# 프로젝트 규칙 (루트)

이 저장소는 키움증권 REST API 기반 자동 주식 매매 봇이다. 사용자는 주식·코딩 모두 초보이므로 구현 단계마다 **무엇/왜/어떻게 확인** 순으로 간단히 설명해라.

## 핵심 원칙
- **Python 3.14 + uv**. 명령은 반드시 `uv run …` / `uv add …` 로 실행.
- **`KIWOOM_MODE=mock` 기본**. 실거래(real) 전환은 사용자의 명시적 지시 없이는 절대 바꾸지 않는다.
- **시크릿 금지**: `.env` 는 커밋 금지. 로그·출력에 AppKey/Secret·토큰 전체를 찍지 않는다(마스킹).
- **전략은 `Signal` 만 반환**. 주문 실행/포지션 기록은 엔진/러너가 담당.

## 작업 시 점검
- 의존성 추가: `uv add <pkg>` (dev 는 `--dev`)
- 테스트: `uv run pytest` / 통합은 `uv run pytest -m integration`
- 실행: `uv run auto-trade …`

## 모듈 지도 (`src/auto_trading/`)
폴더별 책임 1줄 요약. 상세 규칙은 각 폴더 `CLAUDE.md`.

| 폴더 | 책임 |
|------|------|
| `config.py` | 설정 단일 출처(`get_settings()`), mock/real·시크릿 |
| `kiwoom/` | 외부 키움 API 단일 경유(REST/WS/토큰) |
| `market/` | 시세 read-only (Bar/Quote/Tick) |
| `account/` | 잔고·주문 (무분기 호출) |
| `strategy/` | 판단 = `Signal` 만 |
| `risk/` | 필터·수량 결정 |
| `runner/` | 라이브 루프 + **실거래 가드** |
| `daytrade/` | 일일 발굴·게이트 오케스트레이션 |
| `backtest/` | 결정론 시뮬레이션·지표 |
| `reports/` | 사후 집계(daily JSON) |
| `notify/` | 텔레그램·스로틀·상태 포맷 |
| `dashboard/` | read-only 로컬 웹(FastAPI) |
| `llm/` | Claude 비용 추적·월 한도 가드 |
| `news/` | 주간 RSS·LLM 제안·프로파일 자동갱신 |
| `monitor/` | 지연·데이터 정합 진단(read-only) |
| `shadow/` | 주문 없는 가상 PnL 추적 |
| `cli/` | 진입점·인자 파싱(`auto-trade`) |

## YAML 프로파일 / 주간 자동화
- 전략 설정은 코드 대신 YAML 편집 우선(사용자 선호). swing=`StrategyProfile`, day-trade=`DaytradeProfile`, 저장 위치 `strategies/profiles/*.yaml`.
- 주간 흐름: insight(Phase B, LLM) → proposal → `news/profile_writer` 가 `daytrade-current.yaml` 자동 갱신(Phase C, SafetyCaps·`.bak` 백업) → 토요일 cron 텔레그램 푸시(Phase D). LLM 은 사람 명령 또는 주 1회 이하 cron 만.

## 진행 관리
- 큰 변경 전후로 `todo-list.md` 를 갱신해 사용자가 흐름을 파악할 수 있게 한다.
- 각 서브 폴더의 `CLAUDE.md` 는 그 폴더 규칙만 담는다 — 루트 내용을 복제하지 마라.
