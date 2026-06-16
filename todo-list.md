# 할 일 체크리스트 (초보자 가이드)

각 항목은 **무엇을 만드는지 / 왜 필요한지 / 어떻게 확인하는지** 세 줄로 설명합니다.

---

> 완료된 1~29단계 PR 이력(머지 PR 번호·날짜 포함)은 [docs/history.md](docs/history.md) 로 이관. 여기에는 **미완 작업 + 선택 과제만** 남긴다.

## 🔧 진행 중 — 미완 항목

- [x] **일일 리포트 timeline 매도 누락 수정** (2026-06-15)
  - **무엇**: `data/reports/daily-*.json` 의 `timeline[]` 이 매수와 partial_take 가드만 잡고 손절·트레일링·일반 매도·청산 체결을 통째로 빠뜨려 매도 흐름이 착시되던 버그. 파서(`log_parser.py`)에 `position_guard.stop_loss`/`trailing` 추가, 빌더(`daily.py`)가 매도/청산 체결(orders side=sell · liquidations)을 전부 시각순으로 넣고 가드 이벤트로 사유(손절/부분익절/트레일링)를 라벨링. 텔레그램 흐름 섹션에 새 kind 이모지 + 손절 truncate 보존.
  - **어떻게 확인**: 2026-06-15 실로그 재파싱 시 09:11 손절·09:13 트레일링·09:18 부분익절·09:26 트레일링 4건 모두 timeline 에 표시. `uv run pytest`(신규 회귀 5종)·`uv run mypy src` 통과.
  - **후속(같은 날)**: 사용자 요청 — 실시간 체결 텔레그램(`runner.order_accepted`)에 단가 + 총 거래대금(수량×단가) 추가. 직접 포맷 `✅ 체결 접수 005930 삼성전자 BUY 10주 @339,500원 = 3,395,000원`, throttle 요약은 압축 형식 `✅ 005930 BUY 10주 (3,395,000원)`. 러너가 notify 에 `price` 전달하도록 배선. price 없으면 금액 생략(회귀 보호). 신규 테스트 4종.

- [x] **제3자 에이전트 검토 후속 — 주문·안전장치·지표 버그 일괄 수정** (2026-06-10)
  - **무엇**: 독립 검토 에이전트 3개(전략/실행/백테스트)가 찾은 코드 버그 수정. ① 주문 TR 의 5xx 자동 재전송 제거(이중 체결 방지) + 타임아웃/5xx 주문은 `unknown` 저널로 복구 스캔에 잔류, ② 일일 청산 시작 후 신규 매수/guard 중복 매도 차단, ③ `daily_loss_limit` 0=무제한 규약 통일(종전엔 0 이 "보합 이하 전부 차단·즉시 강제청산"), ④ 매수 거절 시 전략 진입기록 롤백(종목 하루 잠김 해소), ⑤ 일일 리포트 회전율 등 6필드 복원 유실 수정, ⑥ 분봉 매도체결량 절사 수정, ⑦ 백테스트 SELL weight 비례 매도(라이브 일치)·재실행 격리, ⑧ 승률 매수수수료 안분·shadow 매수수수료 누락(PnL 정의 통일), ⑨ 추가매수 평단 가중평균, SELL weight 0 거절.
  - **왜**: 실거래 전 필수 안전성(①~④) + 의사결정 지표 신뢰(⑤~⑧). 전략 파라미터 변경(발굴 거래대금 전환·churn 임계 활성화·레짐 임계 재보정)은 ledger 반사실 검증 후 별도 진행 — 이번 PR 에 포함 안 함.
  - **어떻게 확인**: `uv run pytest` 1170 통과(신규 24). 모의 운영에서 `runner.signal_blocked_liquidation` / `order_journal` 의 `unknown` 이벤트 / 리포트 JSON 의 `turnover_ratio` 채워짐 확인.

- [x] **제3자 검토 잔여 2차 — 러너 보호 보강** (2026-06-11)
  - **무엇**: ① kill switch·잔고 갱신을 tick 루프에서 독립 task(`_protection_loop`)로 분리 — WS 단절·전종목 거래정지 중에도 daily_loss_kill 이 돈다. ② `_pending_buys` 대칭 보정 — BUY 접수 후 키움 잔고 미반영 구간의 보호 공백·중복 비중 사용 차단. ③ `day_state` 디스크 영속 — 장중 비정상 재시동 시 일일 손실 baseline·매수대금·BUY 카운트·churn 누적 복원(한도 희석 방지). ④ **주문 저널이 러너에 배선돼 있지 않던 운영 공백 수정** — paper/paper-day 에 `OrderJournal`+`DayStateStore` 주입(복구 스캔·preflight 게이트가 이제 실제 데이터를 본다). ⑤ 분봉 백테스트 Sharpe 연환산(봉 간격 추정) + 하루 미만 곡선 CAGR OverflowError 수정.
  - **어떻게 확인**: `uv run pytest` 1190 통과(신규 20). 다음 paper-day 후 `data/orders/state.jsonl`·`data/state/day-state.json` 생성 확인. 장중 재시동 시 로그 `runner.day_state_restored`.

- [x] **churn 브레이크 임계 활성화 — ledger 역산 완료, -15만원** (2026-06-11)
  - **무엇**: 5월 전 로그를 FIFO 재구성해 일중 누적 실현손익(NET) 곡선 복원. 임계 -15만원이 유일하게 5/12 폭주일만 잡고(13:13 KST 도달 → 이후 추가 손실 약 -26.4만원 차단) 나머지 날은 한 번도 안 걸림. -10만원은 5/26(승자 재진입일, 이후 +10.4만원 회복)을 09:15 에 오발동 — 탈락. trend/range/defensive/default 프로파일에 `daily_realized_loss_limit: 150000` 적용.
  - **한계**: 표본 6거래일(매도 발생일 기준) — forward 관측으로 계속 검증. 차단은 신규 매수만이고 보유분 손익은 그대로라 "이후 손실 전액 절약"은 상한 추정.

- [x] **레짐 분류 임계 — 실측 결과 재보정 불필요** (2026-06-11)
  - **무엇**: KODEX 200 최근 252거래일에 `scanner.classify()` 롤링 적용 → trend_up 44% / high_vol 29% / range 26%. 제3자 검토의 "거의 전부 RANGE 로 퇴화" 우려는 실측으로 반박됨(2025~26 강세장에서 slope 중앙값 0.43%/일로 임계 0.3% 를 충분히 넘음). 임계를 낮추면(slope 0.001·atr 0.02) crash 가 57%로 과발동 — **현행 임계 유지**.

- [x] **발굴 랭킹 축 검증 — 거래대금 전환 근거 없음, 현행 유지** (2026-06-12, `scripts/diagnostics/ranking_axis_study.py`, 결과 `data/studies/ranking_axis_study_20260612_002331.json`)
  - **무엇**: 거래량 상위 40 바스켓 x 3년(729거래일), 4가지 선별(거래량(현행)/거래대금/거래대금+최소2천원/거래량+필터)의 당일 시가→종가 수익 페어드 비교. 합성 데이터 자기검증(가짜 신호 생성/누락 없음) + 랭킹 시점 프록시 2종으로 룩어헤드 양방향 검증.
  - **결과**: ① 당일 거래량 랭킹(same_day, 약한 룩어헤드 — 봇의 09:06 랭킹에 가까움): 현행이 가장 좋음(거래대금 -0.36%p/일 유의 악화). ② 전일 기준(prev_day, 룩어헤드 0): 거래대금 +0.05%p/일이지만 p≈0.45 로 불확실. **두 모드 방향 불일치 → 사전 규칙대로 전환 안 함.** 제3자 검토의 "주식수 랭킹=동전주 함정" 가설은 이 데이터에서 지지되지 않음.
  - **부수 발견**: prev_day 모드에서 어떤 랭킹이든 픽 평균이 바스켓 평균보다 좋지 않음 — 전일 활동량 자체에는 선별 엣지가 없고, 있다면 "그날 아침" 모멘텀 성분(프록시가 못 잡는 부분)에 있음. 분봉 데이터 확보 시 09:06 시점 재현 검증이 다음 단계.
  - **확장 검증 (2026-06-15, 같은 스크립트 + 상대거래량·모멘텀 축 추가, 결과 `data/studies/ranking_axis_study_20260615_213504.json`)**: 사용자 "매번 삼성전자" 가설의 대안 축으로 **상대거래량**(전일 거래량/직전 20일 평균)·**모멘텀5일**(직전 5일 종가 수익률) 추가. prev_day(룩어헤드 0): 거래량 baseline −0.149%/일 대비 거래대금 +0.073%p(p=0.174)·**상대거래량 +0.042%p(p=0.514, 대안 중 가장 약함)**·모멘텀 +0.123%p(p=0.144) — **전부 비유의(CI가 0 포함)**. 즉 **상대거래량 전환은 데이터 근거 없음**, 모멘텀이 그나마 점추정 1위지만 비유의. **랭킹 축 무변경 유지.** "한 종목 쏠림"은 축이 아니라 top_n(분산) 문제 — crash(top_n=1) → highvol(top_n=3) 라우팅 교체로 이미 완화([[project_highvol_active_routing]]). top_n↑은 concentration_study 상 변동성↓이나 비용↑ 트레이드오프라 현행 3 유지.

- [ ] **제3자 검토 잔여 — 낮은 우선순위** (2026-06-10)
  - 진입 직전 종목 자체 필터(분봉 필요), CPCV fold 경계 갭(비연속 fold concat 시 인공 PnL — 경계 강제청산+전략 reset 설계 필요).

- [ ] **누적(전날) 추세로 선별 개선 — 효과 검증 먼저** (2026-06-03, 승인된 플랜 `vivid-crunching-donut`)
  - **무엇**: 사용자 문제의식("당일 지표만 보지 말고 전날까지 추세를 보면 더 낫지 않나") 적대적 재검토 결과 — 현 봇은 종목별로 사실상 *무조건 매수*라 추세 게이트는 거래만 줄이는 미검증 베팅. 사용자 선호 = **막지 말고 선별/진입 개선**(거래수 유지). 1순위로 "거래량 상위 풀을 전날까지 추세점수로 재정렬(gate 아님 ranking)"의 효과부터 읽기전용으로 측정.
  - **산출물**: `scripts/diagnostics/selection_study.py` — 12개월 일봉으로 baseline(거래량만) vs improved(거래량+추세) vs pool 의 당일 시가→종가 수익률·승률·페어드 t 비교, `data/studies/*.json` 저장. 합성데이터 자기검증 통과(추세가 수익을 예측하는 세계에서 improved가 baseline 능가, 거래량 함정 회피 확인).
  - **✅ 실데이터 실행 완료 (2026-06-07, `data/studies/selection_study_20260607_183103.json`)**: baseline(거래량만) 평균 -0.209%/일 vs improved(+추세 리랭킹) -0.300%/일 — **추세 리랭킹이 오히려 나쁨**(paired diff -0.090%p, t=-1.01, boot p=0.284, CI 가 0 포함). 사전 합의 규칙(|t|<2·diff≤0 → 구현 안 함)에 해당 → **구현 안 함, 현행 유지** (2026-06-11 확인). 집중도 연구(top3 vs top10)도 유의차 없음(p=0.35).
  - **부수 발견**: pool(상위 10%) 평균이 top-pick 보다 덜 나쁨 — "거래량 상위 압축" 자체가 음의 엣지일 가능성. 발굴 개선은 리랭킹이 아니라 **랭킹 축 교체(거래대금)+필터** 방향으로 검증하는 게 다음 후보.

- [ ] **수익률 재검토 — 리포트 보정 완료, 파라미터 변경은 보류** (2026-05-31, [docs/profit-review-2026-05-30.md](docs/profit-review-2026-05-30.md))
  - **무엇**: 5일치 거래를 라운드트립 재구성(`scripts/diagnostics/analyze_trades.py`) + 제3자 독립 검증 2명. 결과 보정: 손실의 거의 전부가 **단 하루(5/12, −448k)**에서 나왔고 **나머지 4일은 비용 차감 후 흑자(+73,976)**. 초안의 "구조적 적자" 프레이밍은 5/12에 끌려간 과장이라 정정.
  - **검증으로 폐기된 처방**: A(종목당 1회)는 반사실에서 **해로움**(5/26 재진입 승자 +107,265 제거 → 그날 −106,301). 트레일링 지연발동도 하방 위험·무근거로 폐기. B(하루 진입 상한)만 데이터상 유효(+63,836)하나 임계 과적합 → 라이브 전 ledger 검증 필요.
  - **사용자 결정(2026-05-31)**: **리포트 보정만 채택. 코드·설정·YAML 전부 무변경.** B·킬스위치 규명·스윙은 추후 별도 결정.
  - **킬스위치 규명 완료**([docs/killswitch-review-2026-05-31.md](docs/killswitch-review-2026-05-31.md)): −3% 킬이 5/12를 못 막은 건 버그가 아니라 그물코가 큼(계좌 4,897만원 대비 5/12 −0.92%, −1%에도 미달). 계좌 %-킬은 churn 출혈에 부적합. 개선안 A는 **2026-05-31 구현 완료 — fail-safe**(무방비 시 새 매수 자동 차단 + 텔레그램 1회 경고). 알림만으론 일과시간 대응 불가라는 사용자 지적 반영해 자동 진입 차단까지. `kill_switch_baseline_missing`(manager) + 매수 게이트 차단 + `runner.{kill_switch_blind,buy_blocked_kill_switch_blind}`, 단위테스트, 전체 1129 통과. B(churn 브레이크)도 **2026-05-31 구현 완료(기본 비활성)**: 그날 누적 순실현손익(NET=비용 차감) −X원 도달 시 신규 BUY 차단(`daily_realized_loss_limit`, 0=비활성, 6개 프로파일 미설정). NET 필수(GROSS는 5/26 승자 잘림). `_daily_realized_net` 적립 + 매수 게이트, 전체 1135 통과. **남은 후속: 임계값을 며칠 shadow/paper 관측으로 forward 설정 후 사용자 승인 하에 활성화**(5일 표본 과적합 회피).


- [x] **HIGH_VOL 라우팅 — 전면정지(crash) → 소액 능동매매(highvol)** (2026-06-14)
  - **무엇**: `REGIME_PROFILE_MAP[HIGH_VOL]` 을 `daytrade-crash`(1원 사이징=봇 완전정지) → 신규 `daytrade-highvol.yaml`(top_n=3·손절 -1.0%·일매수 10M·포지션 비중 7%·노출 40%·churn 10만)로 교체. crash.yaml 은 진짜 패닉장 수동 킬스위치로 보존. 사용자 노출 메시지(`_REGIME_DESCRIPTIONS[HIGH_VOL]`)도 갱신.
  - **왜 (실측)**: ATR/종가 ≥ 3% 면 무조건 HIGH_VOL 인데 최근 시장이 2주(05-29~06-12) 내내 그 위 → 단 한 번도 진입 못 함. 결정적으로 **05-29·06-12 는 breadth 게이트가 강세일(상승종목 63%·76%, 평균 +1.2%·+3.1%)로 통과시키고 지수게이트도 통과**했는데 crash 스위치가 주문을 0원으로 만들어 좋은 날을 놓침. breadth 게이트는 이미 나쁜 날을 정확히 거른다고 실측 검증(`project_breadth_gate_verified`)된 장치 → crash 전면정지는 나쁜 날엔 중복·좋은 날엔 손해.
  - **반사실 검증(12거래일 로그)**: HIGH_VOL→highvol 적용 시 breadth AND 지수게이트 둘 다 통과한 날(05-22·05-26·05-29·06-12)만 진입 가능, 나머지(breadth 차단 또는 지수<SMA)는 그대로 회피. 즉 좋은 날 잡고 나쁜 날 회피로 동작. (레짐 임계 자체는 유지 — 2026-06-11 "재보정 불필요" 결론과 일관.)
  - **어떻게 확인**: `uv run pytest -m 'not integration'` 1194 통과 + ruff + `mypy src` 통과. 다음 paper-day 시동 시 콘솔 `📊 시장 국면 자동 라우팅: 고변동성 → daytrade-highvol.yaml`. (남은 과제: 상대거래량/모멘텀 랭킹 축은 `ranking_axis_study` 확장으로 별도 검증 후 결정.)

- [ ] **시장국면 자동 라우팅 — 사용자 검증** (2026-05-28 구현 + 운영 ON)
  - **무엇**: paper-day 시동 시 KODEX 200(069500) 60일봉 → `scanner.classify()` → TREND_UP/DOWN/RANGE/HIGH_VOL 4분류 → `select_profile_for_regime()` 가 매핑된 daytrade-{trend,defensive,range,crash}.yaml 로 자동 스위치. 매핑된 4개 프로파일은 `daytrade-current.yaml` 베이스 + 시장국면별로 `top_n`/`stop_loss_pct`/`daily_notional_limit`/`max_buys_per_symbol` 만 보수/공격 방향 조정 (새 파라미터 도입 X).
  - **왜**: 약세장(TREND_DOWN)·고변동성(HIGH_VOL) 에 같은 공격적 프로파일을 그대로 쓰면 과매매·비용 누수. 분류 결과에 따라 노출 자동 축소·매수 차단. 사용자 메모리 `project_after_cost_loss` "회전율 1순위" 원칙 강화. enabled=False(default) 면 회귀 0.
  - **현재 상태**: `strategies/profiles/daytrade-current.yaml` 의 `regime_router.enabled: true` (2026-05-28 사용자 요청으로 켬). 직전 yaml 은 `*.bak.2026-05-28-pre-regime-on` 보관. 끄려면 false 로 한 줄 변경 또는 백업 복원.
  - **fail-safe**: KODEX 200 fetch/분류 실패 또는 매핑 파일 부재 시 현재 프로파일 유지 + 콘솔 경고. 봇은 안 멈춤.
  - **토요일 cron 영향 없음**: `apply_proposal` 이 proposal 의 명시 필드(top_n/params/risk/avoid_symbols)만 갱신, `regime_router` 블록은 보존.
  - **어떻게 확인**:
    1. 다음 paper-day 시동 시 콘솔에 `📊 시장 국면 자동 라우팅: <라벨> → daytrade-X.yaml (...). 👍 ... 👎 ...` 1줄 출력.
    2. 이어서 `프로파일 로드: daytrade-X.yaml (... top_n=N ...)` 표시 — top_n 이 시장국면에 맞춰 변동.
    3. fail-safe 케이스(시장 휴장·fetch 실패) 면 `⚠️ 시장 국면 분류 실패` 또는 `📊 시장 국면=X 이지만 매핑 프로파일이 없어 현재 유지` 메시지.
    4. 단위 + 통합 테스트: `uv run pytest tests/test_strategy_regime_router.py tests/test_daytrade_regime_router_integration.py -v` (12 케이스 통과).
  - **운영 관찰 포인트**: 4개 매핑 프로파일의 보수성 파라미터(특히 daytrade-defensive 의 top_n=2·daily_notional_limit=10M, daytrade-crash 의 max_buys_per_symbol=0)가 실제 시장 분류와 잘 맞는지 1~2주 관찰. 분류 임계(`scanner.SLOPE_TREND_THRESHOLD=0.003`, `ATR_HIGH_VOL_THRESHOLD=0.030`) 조정은 별도 PR.



- [ ] **회피 종목 + 회전율 가드 — 사용자 검증** (2026-05-26 구현, mock 검증 대기)
  - **무엇**: 두 가지 가드를 한 번에 도입했다.
    1. `DaytradeProfile.avoid_symbols` (목록) — 발굴 단계에서 거래량 상위라도 후보에서 즉시 제외. orchestrator 가 `("코드","avoid_symbols")` 로 blocked 마킹.
    2. `risk.max_buys_per_symbol` (정수, 0=비활성) — 종목당 하루 BUY 체결 횟수 한도. 한도 도달 시 새 BUY 시그널 차단(`runner.signal_blocked_buy_limit`). SELL/청산은 영향 없음. dry_run BUY 도 같은 카운터로 누적해 라이브와 회로 동일. KST 날짜 바뀌면 자동 리셋.
  - **왜**: 2026-05-26 운영에서 (a) 주간 LLM 의 `⛔ 회피 종목 356680 엑스게이트` 권고가 발굴에 반영 안 돼 4회 매수, (b) 같은 종목 3~4회 반복 진입으로 회전 0.7배 + 비용 차감 -20,488원 — 두 패턴이 동시에 나타남. 둘 다 다음 거래일부터 막을 1차 가드.
  - **현재 프로파일**: `strategies/profiles/daytrade-current.yaml` 에 `avoid_symbols: ['356680']` + `risk.max_buys_per_symbol: 2` 활성. 직전 yaml 은 `*.bak.2026-05-26` 보관.
  - **주간 자동화 연동**: `DaytradeProposal` 에 `max_buys_per_symbol` 추가, `profile_writer` 가 LLM 의 `symbols_to_avoid` 를 YAML 의 `avoid_symbols` 로 자동 반영. `SafetyCaps` 에 `max_buys_per_symbol_max=10`, `avoid_symbols_max_len=30` 캡 신설.
  - **어떻게 확인**:
    1. 다음 거래일 paper-day 시동 후 텔레그램 `🔎 발굴 완료` 에 `356680` 미포함, 로그 `daytrade.discovery blocked` 에 `("356680","avoid_symbols")` 표시.
    2. 같은 종목 2회 BUY 후 3번째 시그널 시 로그 `runner.signal_blocked_buy_limit symbol=… count=2 limit=2` 출력 + 텔레그램에 추가 체결 알림 없음.
    3. 일일 리포트의 종목별 매수 건수가 모든 종목 ≤2 인지.
    4. 단위 테스트: `uv run pytest tests/test_daytrade_orchestrator.py tests/test_daytrade_profile.py tests/test_profile_writer.py tests/test_runner_buy_limit.py -q` (모두 통과 — 62 케이스).
  - **운영 관찰 포인트**: 한도가 너무 빡빡하면 정상 추세 종목까지 막혀 기회 손실 가능 — 1~2주 운영 후 회전·비용 변화 보고 한도 조정. 회피 목록은 LLM 권고를 자동 반영하므로, 의도치 않은 차단 발생 시 yaml `avoid_symbols` 에서 빼면 됨.



- [ ] **29단계 PR4 · `feat(account): 키움 미체결 조회 + suspect 자동 매칭 — #9 3차`**
  - **무엇**: 봇 재시작 시 suspect 주문들을 키움에 재조회해 실제 체결 여부 확인 + 저널 보강 + 로컬 state 동기화.
  - **왜**: PR2 가 *발견* 까지만 구현. 자동 매칭/동기화는 키움 미체결 TR 코드 probe 가 선행이라 분리했다.
  - **어떻게 확인**: 키움 미체결 TR probe → 재조회 매칭 → 저널/ state 정합 테스트.

- [ ] **시총 상위 200 스냅샷 — 사용자 검증** (2026-05-21 구현 완료, 첫 시동 성공)
  - **무엇**: KRX 시총 상위 200 종목 마스터 일 1회 갱신(08:50) + 10분 간격 현재가 + KOSPI/KOSDAQ 지수 JSONL 누적(09:00–15:30).
  - **왜**: 시장 전반 추적용 데이터셋 — 추후 대시보드/백테스트 입력으로 사용. 키움 시총 TR 부재 → **FinanceDataReader** 로 KRX 공식 데이터 (당초 pykrx 로 도입했으나 2026-05-21 KRX OTP 다운로드 페이지 변경으로 pykrx 시장 전체 함수 전부 KeyError → fdr 로 이전).

- [ ] **Phase 1 breadth 게이트 — 사용자 검증** (2026-05-21 구현 완료, 기본 비활성)
  - **무엇**: 시총 200 스냅샷의 마지막 라운드에서 advancing_pct(상승종목비율) + mean_change_pct(평균등락률) 산출 → paper-day 발굴 시 매수 게이트로 활용 (약세면 매수 차단). 기본 `enabled=False`.
  - **왜**: 회전율 1순위 정신 — 시장 약세에서는 매수 자체를 막아 비용 누수 차단. 깜깜이 변경 방지 위해 며칠 dry-run 후 임계 조정.
  - **어떻게 확인**:
    1. `strategies/profiles/daytrade-current.yaml` 에 다음 블록 추가 후 paper-day 1~2주 운영:
       ```yaml
       breadth_gate:
         enabled: true
         min_advancing_pct: 0.5      # 200개 중 50% 이상 상승해야 통과
         min_mean_change_pct: 0.0    # 평균 등락률 0% 이상
         snapshot_dir: data/snapshots
       ```
    2. paper-day 로그에서 `daytrade.breadth_gate` 이벤트 + `passed`/`reason` 확인.
    3. 약세 차단 케이스 발생 시 `daytrade.universe_chosen reason=breadth_blocked` 줄 검증.
    4. 데이터 부족(파일 없음) 시 fail-open — 기존 흐름 보존 확인.
  - **다음 단계 Phase 2 발굴 풀 교체**: Phase 1 며칠 관찰 후 시총 200 모멘텀 상위로 발굴 풀 교체 (현재 거래량 상위 3 → 시총 200 모멘텀 상위 N). 별도 PR.
  - **어떻게 확인**:
    1. `bash scripts/launchd-install.sh` → `launchctl list | grep -E "marketcap-universe|snapshot-top200"` 두 라벨 보임.
    2. 다음 평일 08:51 직후 `data/snapshots/universe-YYYY-MM-DD.json` 200건 생성 확인.
    3. 같은 날 09:00 이후 `data/snapshots/topcap-YYYY-MM-DD.jsonl` 가 10분마다 ~200줄씩 증가.
    4. `tail -1 data/snapshots/topcap-YYYY-MM-DD.jsonl | python -m json.tool` 스키마 확인.
  - **수동 1회 트리거**: `launchctl kickstart -k gui/$UID/com.mizeloble.auto-trade.marketcap-universe`

- [ ] **`auto-trade ops` — LaunchAgent 자동실행 작업 가시성 (Phase 1) — 사용자 검증** (2026-05-25 구현 완료)
  - **무엇**: `uv run auto-trade ops` 로 launchd 자동실행 작업 상태(Label / 마지막 실행 / Exit / 비고)를 한 화면 표 출력. `launchctl print` 의 `last exit code` + `logs/launchd-*.{out,err}.log` mtime 두 원천. Exit 표기: `0`=정상, 빨강 정수=비정상, `·`=이번 부팅 후 미실행, `-`=정보 없음.
  - **왜**: "자동실행 작업이 많고 의도대로 도는지 모르겠다" 페인포인트 해소. 회의적 검토 결과 단순화·통합보다 가시성 먼저 (신뢰 없이 리팩토링하면 회귀 위험). churnfix-verify 같이 만료된 1회성은 비고에 "⚠ 만료된 1회성, 별도 정리" 표시 — 실제 정리는 별도 작업.
  - **어떻게 확인**:
    1. `uv run auto-trade ops` → 등록된 plist 수만큼 행 출력. 빈 디렉토리면 안내 메시지.
    2. heartbeat-check (5분 간격) 가 `0`, 평일 아침에만 도는 잡들이 부팅 직후엔 `·`.
    3. `uv run pytest tests/test_cli_ops.py -v` → 5 케이스 PASS.

- [ ] **`auto-trade ops` Phase 2 — 추후 보강** (선택, Phase 1 운영 1~2주 후 별도 plan)
  - **무엇**: ① "다음 예정 시각" 칸(plistlib + launchd 캘린더 시뮬레이션), ② "7일 예상 vs 실제 실행 횟수" 통계, ③ 텔레그램 `/ops` 명령(러너 떠 있을 때만 응답하는 비대칭 인지), ④ `scripts/launchd/schedules.yaml` 단일 출처에서 plist 8개 자동 생성.
  - **왜**: 가시성 → 신뢰 → 구조 단순화 순서. 한 번에 너무 많이 하면 캘린더 로직 버그·로그 rotate 충돌 등 회귀 위험.
  - **어떻게 확인**: 본 plan은 Phase 1 운영 안정 후 작성.

---

## 🚀 남은 과제 (선택)

- **23~24단계 사용자 검증** · 다음 평일 paper-day 시동 후 콘솔 "프로파일 로드:" + 일일 리포트 비용 차감 PnL 라인 확인. 토요일 cron 후 텔레그램에 "🗞 다음 주 day-trade 프로파일 갱신" 도착 확인. 다음 월요일 paper-day 가 `daytrade-current.yaml` 자동 사용 확인.
- **26단계 사용자 검증** · 며칠 paper-day 가 돈 후 `auto-trade dashboard` 로 자산곡선·일별 상세가 그려지는지 확인. Phase B 어떤 페이지가 가장 필요한지 결정 후 후속 PR.
- **첫 주 한도 활성** · 며칠 dry-run 으로 로그·푸시 정상 확인 후, `daytrade-default.yaml` 또는 `daytrade-current.yaml` 에서 한도 1~2개부터 켠다 (예: `stop_loss_pct=0.01`). 한 번에 다 켜지 말 것.
- **RSS feed 추가 (잔여)** · 27단계로 한경+연합+SBS Biz+파이낸셜뉴스 6개로 확대. 이데일리·머니투데이·서울경제 RSS 가 안정화되면 복귀, 매일경제도 동일.
- **실거래 전환** · `.env` 에 실계좌 키 + `KIWOOM_MODE=real` + `--allow-real` (사용자 명시 승인 후). 사전 검토: 집 맥북 launchd 의존이 실거래 신뢰성에 충분한지 — 부족하다고 판단되면 그 시점에 systemd(Oracle 무료/한국 리전) 이전을 같이 진행.
