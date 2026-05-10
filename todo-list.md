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

## 🛡 29단계 · 실거래 전환 안전망 — Top 5 PR

자세한 설계: `~/.claude/plans/calm-cooking-pelican.md` (23개 후보 + 검증 + 28단계와의 관계).

- [x] **PR1 · `feat(account): 주문 저널 + client UUID — #9 1차`**
  - 새 모듈 `account/order_journal.py` (JSONL append-only, replay).
  - `place_order(...)` 에 `journal: OrderJournal | None`, `client_id: str | None` keyword-only 인자 추가. 미지정 시 자동 UUID. journal 미지정이면 기존 동작 그대로 (회귀 0).
  - `OrderResult.client_id` 필드 추가 (default `""`).
  - 테스트 13건 추가 (저널 append/replay/concurrency + client_id 자동/수동 부여 + journal 통합 + 실패 경로).
- [x] **PR2 · `feat(runner): 저널 주입 + 시동 시 손실 의심 주문 알림 — #9 2차`**
  - 새 모듈 `account/recovery.py` (`SuspectOrder` + `find_suspect_orders` 순수 함수).
  - `PaperRunner.journal: OrderJournal | None` 필드 추가 (default None — 기존 호출 호환).
  - 시동 시 `_warn_recovery_pending()` — replay → suspect → 텔레그램 `runner.recovery_pending` 푸시 (client_id 앞 8자만 노출).
  - `_place_and_track` + `liquidate_all` 의 place_order 호출에 `journal=self.journal` 전달.
  - 테스트 15건 추가 (recovery 순수 함수 9건 + runner 통합 6건). 542 전체 통과.
  - **자동 키움 미체결 조회 + 매칭 + 로컬 state 동기화는 PR4 로 분리** — 이번 PR 은 *발견* 까지.
- [x] **PR3 · `feat(runner): heartbeat dead-man switch — #13`**
  - 새 모듈 `auto_trading/heartbeat.py` — `Heartbeat.write()` (atomic mtime 갱신) + `check_heartbeat()` 순수 함수.
  - `PaperRunner.heartbeat: Heartbeat | None` 필드 + `_heartbeat_loop()` background task. 시동 직후 1회 즉시 write, interval_sec(기본 1800s) 마다 갱신, STOPPING 진입 시 다음 사이클에 종료, cancel 깨끗이 처리.
  - 새 CLI `auto-trade heartbeat-check` — 누락 / stale / 정상 분류, cooldown(기본 1800s) 으로 spam 차단, 텔레그램 `runner.heartbeat_alert` 푸시.
  - 테스트 16건 추가 (heartbeat write/swallow/check 7건 + runner background task 4건 + CLI 5건). 558 전체 통과.
  - **launchd plist 등록은 사용자 수동 작업** — 5분 간격으로 `heartbeat-check` 실행하는 cron 추가 시 운영 활성.
- [ ] **PR4 · `feat(account): 키움 미체결 조회 + suspect 자동 매칭 — #9 3차`** — 봇 재시작 시 suspect 들을 키움에 재조회해 실제 체결 여부 확인 + 저널 보강 + 로컬 state 동기화. 키움 미체결 TR 코드 probe 후 진행.
- [x] **PR13 · `feat(llm): 비용 누적 추적 모듈 — #16 1차`**
  - 새 모듈 `llm/cost_tracker.py` — `CostTracker` (JSONL append-only, account/order_journal 패턴 재사용) + `CostEntry` / `MonthlySummary` / `BudgetStatus` 데이터클래스.
  - 모델별 가격표 `MODEL_PRICES_USD_PER_M` (Opus/Sonnet/Haiku 4.x 계열). 알 수 없는 모델은 가장 비싼 default(Opus) 가격 — 한도 보호.
  - `record(model, input_tokens, output_tokens)` / `monthly_summary(year_month)` / `budget_status()` 인터페이스. 월 경계는 KST 기준.
  - default 한도 $5/월 (memory cost discipline) + alert 80% / over 100% 분기.
  - 디스크 실패는 swallow + 로그 — LLM 호출 성공을 막지 않게.
  - 테스트 19건 (가격 5 + record/replay 5 + monthly_summary 3 + budget_status 5 + 손상 1). 669 전체 통과.
  - **호출 지점 wiring 은 PR14 로 분리** — `news/proposal.py` / `notify/insight.py` / `strategy/llm_advisor.py` 3곳에 record() 추가 + preflight `llm_cost_within_budget` 게이트 활성.
- [ ] **PR14 · `feat(llm): 비용 추적 wiring + preflight 게이트 활성 — #16 2차`** — 3개 호출 지점에 cost_tracker.record + budget alert/over 시 텔레그램 + LLM 차단.
- [x] **PR12 · `feat(kiwoom): OAuth 토큰 디스크 캐시 — #21`**
  - `TokenProvider.cache_path: Path | None` 추가 — 시동 시 자동 로드, 발급 후 자동 저장, invalidate 시 자동 삭제.
  - 만료 마진(REFRESH_MARGIN=10분) 안의 캐시는 무시하고 새로 발급. 손상된 JSON / 형식 오류는 swallow + 로그 → 정상 발급 fallback.
  - 시크릿 파일 권한 0o600 (POSIX 만, Windows 는 무시).
  - `Settings.kiwoom_token_cache_path: Path | None = None` 추가 (default 비활성). `.env` 의 `KIWOOM_TOKEN_CACHE_PATH=data/auth/token.json` 으로 활성. `data/` 는 .gitignore 자동 제외.
  - 명시 인자 > Settings default 우선순위.
  - 테스트 8건 추가 (저장/로드/만료/손상/invalidate 삭제/POSIX 권한 0o600/Settings default None/명시 우선). 650 전체 통과.
- [x] **PR5 · `feat(account): 거절 메시지 분류 — #10 1차`**
  - 새 모듈 `account/reject_classifier.py` — `RejectCategory` (상한가/하한가/VI/거래정지/휴장/종목무효/잔고부족/UNKNOWN) + `classify_reject(message, code)` 순수 함수 + 카테고리별 권장 skip 시간.
  - 한글 패턴 + 알려진 키움 코드(RC4010, 1902) 매칭. 'VI' 는 영문 단어 경계 lookaround (`(?<![a-z])vi(?![a-z])`) 로 'device' 같은 false positive 차단.
  - 단위 테스트 22건 (parametrize 17 + skip_seconds + code precedence + None 안전 + 영문 단어 경계 + 시스템 에러 false positive). 580 전체 통과.
  - **runner 통합은 PR6 으로 분리** — KiwoomError catch → classify → universe skip + 텔레그램 카테고리 알림.
- [x] **PR6 · `feat(runner): reject 분류 → 종목 skip + 알림 카테고리 — #10 2차`**
  - `PaperRunner._skipped_until: dict[str, datetime]` 필드 + `_is_symbol_skipped()` 헬퍼 (만료 항목 자동 청소).
  - `_place_and_track` except 블록에서 `KiwoomError` 면 `classify_reject(message, code)` 호출 → skip_seconds > 0 이면 `_skipped_until[symbol] = now + delta` 등록. 일반 예외는 category=UNKNOWN, skip 0.
  - `runner.order_failed` 알림에 `category` + `skip_seconds` 추가 — 텔레그램에서 "왜 거절됐는지" + "언제 재시도" 즉시 인지.
  - `_evaluate_signal` 시작에 skip 검사 — skipped 종목은 risk 평가 전에 차단, `runner.signal_skipped_reject` 로그.
  - 테스트 9건 (KiwoomError 카테고리별 등록/미등록 + 일반 예외 + skip 검사 자동 청소 + signal 차단). 589 전체 통과.
- [x] **PR7 · `test: paper-day 풀 사이클 통합 테스트 — #22`**
  - 새 파일 `tests/test_paper_day_full_cycle.py` — respx mock 키움 + PaperRunner / RiskManager / OrderJournal / classify_reject 가 함께 도는 시나리오 3건.
  - **시나리오 1** (정상 매수): BUY 시그널 → mock 200 OK → 포지션 갱신 + 저널 submit/accepted + notifier `order_accepted`.
  - **시나리오 2** (상한가 거절 → skip): 첫 봉 거절 → `_skipped_until` 등록 → 두 번째 봉 시그널은 차단 (place_order 1회만).
  - **시나리오 3** (네트워크 5xx): 일반 예외라 skip 등록 X, 알림 category=unknown.
  - 기존 `tests/integration/` 은 실 키움 API 의존 — 이 파일은 mock 만 → 매번 도는 테스트.
  - 592 전체 통과.
- [x] **PR9 · `feat(risk): 연속 손절 자동 일시중지 트래커 — #14 1차`**
  - 새 모듈 `risk/consecutive_loss.py` — `ConsecutiveLossTracker` 순수 상태 머신.
  - `record_round_trip(pnl)` / `is_paused(now)` / `unpause()` 인터페이스. 만료 자동 청소.
  - `RiskLimits.max_consecutive_losses` (default 0 = 비활성) + `consecutive_loss_pause_sec` (default 86_400) 추가.
  - 단위 테스트 8건 (비활성 / 누적 / 트리거 / 양수 reset / 만료 / 수동 unpause / paused 중 연장 / breakeven). 624 전체 통과.
  - **runner 통합 + 텔레그램 `/unpause` 명령은 PR10 으로 분리** — SELL 후 라운드트립 PnL 산출 + `evaluate` 에서 paused 검사.
- [x] **PR10 · `feat(runner): 연속 손절 트래커 wiring — #14 2차`**
  - `PaperRunner.loss_tracker: ConsecutiveLossTracker | None` 필드 추가, `__post_init__` 에서 `risk.limits.max_consecutive_losses > 0` 면 자동 생성. 명시 주입은 보존.
  - `_evaluate_signal` 시작에 BUY 차단 — paused 면 `runner.signal_blocked_consecutive_loss` 로그 + None 반환. SELL 은 통과 (청산은 허용).
  - `_place_and_track` SELL 성공 분기에 라운드트립 PnL = (ref_price - avg_price) * quantity → `tracker.record_round_trip(pnl)`. 방금 paused 진입했으면 텔레그램 `risk.consecutive_loss_pause` 알림.
  - `unpause_loss_tracker()` 메서드 — 텔레그램 명령용 (PR11).
  - 테스트 12건 (auto-init 3 + SELL 갱신/알림 3 + BUY 차단·SELL 허용 3 + unpause 3). 636 전체 통과.
- [x] **PR11 · `feat(notify): /unpause 텔레그램 명령 — #14 마무리`**
  - `notify/commands/control.py` 에 `handle_unpause` + `UnpauseAction = Callable[[], bool]` 추가. 콜백이 True 반환 시 ack 메시지, False 면 "이미 해제/비활성" 안내.
  - `_dispatch.py` 의 `CommandContext` 에 `on_unpause` 필드 + `dispatch_sync` 에 `/unpause` 분기.
  - `TelegramCommandHandler.__init__` 시그니처 + `factory.build_command_handler` 에 `on_unpause` 인자 추가.
  - `cli.py` 의 paper / paper-day 두 경로에서 `runner.unpause_loss_tracker` 자동 바인딩.
  - `HELP_TEXT` 갱신.
  - 테스트 6건 추가 (handle_unpause 3 + dispatch_sync 3). 642 전체 통과.
- [x] **PR8 · `feat(cli): preflight 명령 — #17`**
  - 새 모듈 `auto_trading/preflight.py` — `GateResult` + 5개 활성 게이트 순수/read-only 함수.
    - `check_risk_limits_active` — daytrade YAML 의 `stop_loss_pct > 0` AND `daily_loss_kill=True`.
    - `check_no_pending_orders` — 주문 저널의 결과 미확인(submit-only) 0건.
    - `check_heartbeat_recent` — heartbeat 파일 mtime ≤ max_age (기본 1일).
    - `check_recent_paper_day_mdd` — 최근 N일 일일 손익률 최저점 ≥ -5%.
    - `check_latest_backtest_sharpe` — 가장 최근 백테스트 Sharpe ≥ 0.
  - 4개 미구현 placeholder (skipped=True): kill-switch 시뮬 / 다채널 알림 ack / LLM 비용 누적 / corporate action 캐시. 향후 PR 에서 활성.
  - 새 CLI `auto-trade preflight --profile … --journal … --heartbeat … --reports-root … --backtests-root …` — rich 표 출력, 모두 통과 시 exit 0 + `--allow-real` 가이드, 하나라도 실패 시 exit 1. `daytrade-current.yaml` 없으면 `daytrade-default.yaml` fallback.
  - 테스트 24건 (preflight 게이트 20 + CLI 4). 616 전체 통과.

---

## 🚀 남은 과제 (선택)

- **23~24단계 사용자 검증** · 다음 평일 paper-day 시동 후 콘솔 "프로파일 로드:" + 일일 리포트 비용 차감 PnL 라인 확인. 토요일 cron 후 텔레그램에 "🗞 다음 주 day-trade 프로파일 갱신" 도착 확인. 다음 월요일 paper-day 가 `daytrade-current.yaml` 자동 사용 확인.
- **26단계 사용자 검증** · 며칠 paper-day 가 돈 후 `auto-trade dashboard` 로 자산곡선·일별 상세가 그려지는지 확인. Phase B 어떤 페이지가 가장 필요한지 결정 후 후속 PR.
- **첫 주 한도 활성** · 며칠 dry-run 으로 로그·푸시 정상 확인 후, `daytrade-default.yaml` 또는 `daytrade-current.yaml` 에서 한도 1~2개부터 켠다 (예: `stop_loss_pct=0.01`). 한 번에 다 켜지 말 것.
- **RSS feed 추가 (잔여)** · 27단계로 한경+연합+SBS Biz+파이낸셜뉴스 6개로 확대. 이데일리·머니투데이·서울경제 RSS 가 안정화되면 복귀, 매일경제도 동일.
- **실거래 전환** · `.env` 에 실계좌 키 + `KIWOOM_MODE=real` + `--allow-real` (사용자 명시 승인 후). 사전 검토: 집 맥북 launchd 의존이 실거래 신뢰성에 충분한지 — 부족하다고 판단되면 그 시점에 systemd(Oracle 무료/한국 리전) 이전을 같이 진행.
