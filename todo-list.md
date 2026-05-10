# 할 일 체크리스트 (초보자 가이드)

각 항목은 **무엇을 만드는지 / 왜 필요한지 / 어떻게 확인하는지** 세 줄로 설명합니다.

---

> 1~26단계 완료 항목은 [docs/history.md](docs/history.md) 로 이관. 각 단계의 머지 PR 번호와 날짜는 거기에 보존.

## 🗞 27단계 · 주말 LLM 전략수립의 뉴스 입력 보강 — **✅ 구현 완료** (2026-05-10)

사용자 피드백: "주말 최신 뉴스를 너무 적게 보고 전략수립 하는것 같아." 24단계가 한경 RSS 2개 · 30건 cap · 240자 truncate 로 시작했는데, 매체 편향 + 잘림이 컸다.

- [x] **RSS 소스 6개로 확대** ([src/auto_trading/news/collector.py:39-51](src/auto_trading/news/collector.py:39))
  - 한경 finance/economy + 연합뉴스 economy/market + SBS Biz 경제 + 파이낸셜뉴스 금융
  - 이데일리·머니투데이·서울경제·매경 RSS 는 2026-05-10 실측 시 빈 응답이라 제외 (코드 코멘트에 기재)
- [x] **LLM 입력 캡 30→80** ([src/auto_trading/news/proposal.py:151](src/auto_trading/news/proposal.py:151)) + `--max-news-items` CLI 옵션 ([cli.py:846](src/auto_trading/cli.py:846))
- [x] **본문 truncate 240→400자** ([collector.py:95](src/auto_trading/news/collector.py:95))
- [x] **캐시 schema v2→v3 + 누적 병합** — `force_refresh=True` 면 기존 캐시 + 신규 fetch 를 url 키 dedupe 후 병합 저장
- [x] **신규 `auto-trade fetch-news` CLI** — RSS 만 미리 받기 (LLM X · 비용 0)
- [x] **토요일 06:00 KST `news-prefetch` cron** ([scripts/launchd/com.mizeloble.auto-trade.news-prefetch.plist](scripts/launchd/com.mizeloble.auto-trade.news-prefetch.plist) + [scripts/news-prefetch.sh](scripts/news-prefetch.sh)) — 09:00 weekly-proposal 의 캐시 선반영
- [x] 테스트 4건 추가 (truncate 400 / 누적 병합 / schema v3 파일명 / `--max-news-items` 흐름 / `fetch-news` 명령 출력)
- [x] README + insight-weekly.sh 주석 갱신, `launchd-install/uninstall.sh` 에 라벨 추가

**비용 영향:** 호출당 input 토큰 ~5k → ~25k, 호출당 약 $0.04 → $0.4~0.5. 주 1회 cron 이므로 **월 약 $0.32 → 월 약 $2** (사용자 합의 상한 $5 이내).

**검증**:
```bash
uv run auto-trade fetch-news --days 7              # data/news/news-v3-*.json 생성, 매체별 건수 표시
uv run auto-trade weekly-proposal --dry-run        # 콘솔 출력에 "최대 80건" 표기 + reports/news 요약
launchctl kickstart -k gui/$UID/com.mizeloble.auto-trade.news-prefetch  # 실 cron 즉시 시동
```

---

## 🧹 28단계 · 코드베이스 리팩토링 — 9개 PR 분해

직전 정리(`9c3d024 chore: swing/probe 분리 + _mask 통합`) 이후 누적된 부채를 작은 PR 단위로 풀어낸다. 한 PR = 한 후보 = 단독 머지/롤백 가능. 외부 동작·공개 API 보존 (순수 리팩토링).

- [ ] **PR1 · `chore(tests): isolated_cwd 픽스처 conftest.py 통합`** — 4개 테스트 파일에서 동일 픽스처 정의 중복 제거. [tests/conftest.py](tests/conftest.py) 신규.
- [x] PR2 · `refactor(dashboard): CSV 로더 파싱 헬퍼 통합` — `_load_equity_csv` / `_load_trades_csv` 의 파일 존재 + OSError swallow 가드를 `_safe_csv_load` 데코레이터로 통합. PEP 695 제네릭(`def _safe_csv_load[T]`) 사용.
- [x] PR3 · `refactor(notify/state): _build_trade_lines 페어링·포맷터 분해` — 75줄 함수를 `_pair_buy_liq_records` + `_format_pair_line/_format_buy_line/_format_sell_line` 으로. 단위 테스트 7건 추가.
- [x] PR4 · `refactor(cli): paper_day 단계별 분해` — 프로파일 덮어쓰기(`apply_daytrade_profile_overrides` + `PaperDayOverrides`) + 러너 wiring(`build_paper_day_runner`) 헬퍼로 추출. paper_day 본체 ~30줄 축소, 단위 테스트 5건 추가.
- [x] PR4b · `refactor(cli): weekly_proposal 단계별 분해` — `news/cli_helpers.py` 신규 (`should_skip_weekly_proposal_for_holiday` + `WeeklyInputs` + `run_weekly_proposal_cycle`). cli.py LOC 1028→1014, 단위 테스트 8건 추가.
- [x] PR5 · `refactor(llm): tool_use 추출 공통 헬퍼` — `news/proposal.py` + `notify/insight.py` + `strategy/llm_advisor.py` 3곳 동일 패턴 통합 (`src/auto_trading/llm/_tool_use.py`). 단위 테스트 6건 추가, src 코드 -65줄.
- [x] PR6 · `refactor(notify): telegram 명령 핸들러 모듈 분리` — `notify/commands/` 패키지(status/control/insight + _dispatch) 신설. `telegram_commands.py` 269→208줄 (-23%). 단위 테스트 21건 추가.
- [x] PR7 · `test(runner): aggregator KST 경계 보강` — 5개 시나리오 중 4개(liquidation/balance/paused/kill_switch 일부)는 기존 테스트로 이미 커버 확인. 진짜 누락된 aggregator 경계 3건만 보강. kill_switch 통합은 PR8 의 인라인 헬퍼 추출과 같이.
- [x] PR8 · `refactor(runner): run() 섹션 주석 + _check_kill_switch 인라인 헬퍼` — 메인 루프에 4개 섹션 주석 + kill_switch 분기를 메서드로 추출. PR7 에서 미룬 kill_switch 단위 테스트 6건 동시 추가. 외부 동작·timing 보존.

**머지 순서**: (PR1 + PR3 + PR5) 병렬 → PR2 → PR4 → PR4b → PR6 → PR7 → PR8.

**검증 (전체 머지 후)**: `uv run pytest` + `uv run ruff check .` + 핵심 명령 dry-run + 1주일 paper-day 운영 모니터.

---

## 🚀 남은 과제 (선택)

- **23~24단계 사용자 검증** · 다음 평일 paper-day 시동 후 콘솔 "프로파일 로드:" + 일일 리포트 비용 차감 PnL 라인 확인. 토요일 cron 후 텔레그램에 "🗞 다음 주 day-trade 프로파일 갱신" 도착 확인. 다음 월요일 paper-day 가 `daytrade-current.yaml` 자동 사용 확인.
- **26단계 사용자 검증** · 며칠 paper-day 가 돈 후 `auto-trade dashboard` 로 자산곡선·일별 상세가 그려지는지 확인. Phase B 어떤 페이지가 가장 필요한지 결정 후 후속 PR.
- **첫 주 한도 활성** · 며칠 dry-run 으로 로그·푸시 정상 확인 후, `daytrade-default.yaml` 또는 `daytrade-current.yaml` 에서 한도 1~2개부터 켠다 (예: `stop_loss_pct=0.01`). 한 번에 다 켜지 말 것.
- **RSS feed 추가 (잔여)** · 27단계로 한경+연합+SBS Biz+파이낸셜뉴스 6개로 확대. 이데일리·머니투데이·서울경제 RSS 가 안정화되면 복귀, 매일경제도 동일.
- **실거래 전환** · `.env` 에 실계좌 키 + `KIWOOM_MODE=real` + `--allow-real` (사용자 명시 승인 후). 사전 검토: 집 맥북 launchd 의존이 실거래 신뢰성에 충분한지 — 부족하다고 판단되면 그 시점에 systemd(Oracle 무료/한국 리전) 이전을 같이 진행.
