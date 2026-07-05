# 할 일 체크리스트 (초보자 가이드)

각 항목은 **무엇을 만드는지 / 왜 필요한지 / 어떻게 확인하는지** 세 줄로 설명합니다.

---

> 완료된 1~29단계 PR 이력(머지 PR 번호·날짜 포함)은 [docs/history.md](docs/history.md) 로 이관. 여기에는 **미완 작업 + 선택 과제만** 남긴다.

## 🔧 진행 중 — 미완 항목

- [x] **notify 리팩토링 — 일일 리포트 포맷터를 `state.py` → `daily_report.py` 로 분리** (2026-07-05)
  - **무엇**: 614줄 `notify/state.py` 에 섞여 있던 두 책임을 분리 — 러너 상태 스냅샷 + 텔레그램 명령 응답(/status 등)은 `state.py`(217줄)에 남기고, 일일 리포트 포맷터(`format_daily_report` + 섹션 헬퍼 전부)는 새 `daily_report.py`(406줄)로 이동. 동작 변경 0줄 — 사용처 import 2곳(telegram.py, cli/_reporting.py)과 테스트 파일명·import 만 갱신.
  - **왜**: 일일 리포트는 dict payload 를, 상태 포맷터는 `RunnerStateSnapshot` 을 받는 서로 무관한 코드인데 한 파일이라 수정 시 서로 딸려 읽혔다. 파일별 책임 1개 원칙 회복.
  - **어떻게 확인**: `uv run pytest` 1272 passed + ruff·mypy 통과. 텔레그램 일일 리포트·/status 응답 포맷 변화 없음.

- [x] **스냅샷 커버리지 확장 — 발굴 종목(시총 200 밖)도 10분 스냅샷** (2026-07-04)
  - **무엇**: topcap 스냅샷이 시총 상위 200 만 담아 거래량 발굴 소형주는 일일 리포트의 진입 등락률·순위·MFE/MAE 가 None 이었고, 반사실 검증 때마다 분봉을 API 로 재조회해야 했다. ① `snapshot-top200` 잡이 매 라운드 끝에 **오늘 발굴 종목**(alt-rank.jsonl 의 거래량 픽/풀 + 거래대금 풀, 시총 200 중복 제외)도 조회해 **별도 파일** `data/snapshots/discovered-YYYY-MM-DD.jsonl` 에 append — breadth 게이트는 topcap 파일 줄 수를 세므로 섞지 않음(오염 방지). ② `reports/snapshot_reader.parse_topcap_for_date` 가 두 파일을 병합 → 일일 리포트의 발굴 소형주 피처가 자동으로 채워짐(builder 무변경). 실패는 본 라운드에 영향 없이 경고 로그만(best-effort).
  - **왜**: 앞으로의 모든 A/B 판정(특히 거래량 vs 거래대금 발굴 비교 — 거래대금 풀 종목까지 스냅샷됨)이 자체 데이터로 가능해지는 기반 작업. 과거 일자는 discovered 파일이 없어 기존과 동일(회귀 0).
  - **어떻게 확인**: 다음 거래일 장중 `wc -l data/snapshots/discovered-*.jsonl` 이 10분마다 ~발굴종목수 증가. 콘솔 "✓ 발굴 종목 스냅샷" 라인. `uv run pytest tests/test_monitor_price_snapshot.py tests/test_reports_snapshot_reader.py`(신규 7케이스).

- [ ] **발굴 대안 신호(거래대금 순위) 데이터 축적 — 몇 주 후 A/B 반사실용** (2026-07-04 시작)
  - **무엇**: 키움 순위 TR 은 당일 것만 조회 가능해 "거래량 vs 거래대금 발굴" 반사실을 과거로 만들 수 없다(선정 알파 검증의 한계). ① `market/rank.py` 에 `top_value()`(ka10030 sort_tp=2, 거래대금 상위 — 병합 정렬은 volume×price 근사) 추가, ② `delayed_discover` 가 발굴 직후 같은 시점에 거래대금 풀(발굴 풀과 같은 크기)을 `data/discovery/alt-rank.jsonl` 한 줄로 축적(`record_alt_ranking`). 매매 동작 무영향 — 조회 실패는 경고 로그만 남기고 삼킴. 기록엔 그날의 거래량 픽/풀도 병기해 파일 하나로 자기완결.
  - **왜**: 검증 시리즈 종결 후 남은 두 경로 중 "새 발굴 신호"의 선행 조건. 지금부터 쌓아야 몇 주 뒤 `selection_alpha_study` 방식(당일 시가→종가 vs KODEX)으로 두 순위의 원재료를 나란히 비교 가능.
  - **어떻게 확인**: 다음 paper-day 장중 `cat data/discovery/alt-rank.jsonl | tail -1 | python3 -m json.tool` → `value_pool` 에 종목 목록. 로그 `daytrade.alt_rank_recorded`. `uv run pytest tests/test_market_rank.py tests/test_daytrade_cli_helpers.py` (신규 3케이스).
  - **후속(미완 사유)**: 거래일 ~15일 쌓이면 비교 스크립트(`scripts/diagnostics/`) 작성해 판정 — 그때까지 이 항목은 열어 둠.

- [x] **진입 지연 단축 반사실 검증 — 지지 없음, 2026-07-04 검증 시리즈 종결** (2026-07-04, diag)
  - **무엇**: `scripts/diagnostics/entry_delay_study.py` — 비대칭 출구 검증의 새 단서("엣지가 개장 첫 10분에 소멸, 모멘텀 게이트가 8분 대기") 후속. 실제 픽 33건에 출구는 현행 고정, 진입 시각만 09:02(발굴 직후)/09:05/09:10(현행)/09:15 그리드. 리포트 buys 로 "실제 진입 26 vs 게이트가 종일 차단 7"을 분리해 게이트의 날 거르기 효과도 근사. **결과: 지지 없음** — 09:02 는 평균만 근소 우위(−0.01% vs −0.24%)이고 중앙값(−1.15% vs −0.46%)·승률(6/26 vs 9/26)은 나쁨, 그리드 비단조(09:05 가 최악) = 노이즈 패턴. 조기 진입은 개장 변동에 트레일링이 더 자주 잘림. 게이트 차단 픽의 기회비용은 +0.45%로 소폭(그중 1건은 매매제한이라 실질 더 작음). '첫 10분 엣지'의 상당 부분은 09:00~09:02(동시호가~첫 분봉) — 발굴 파이프라인이 구조적으로 못 잡는 구간. **진입 시각·게이트 무변경.**
  - **왜(시리즈 종결)**: 2026-07-04 적대적 검토발 반사실 검증 6종 완료 — 쿨다운·진입 지연(양방향)·ATR 출구·비대칭 출구·선정(풀/순위) 전부 "현행 유지" 결론. 현행 전략은 검증한 모든 축에서 로컬 최적. 남은 개선 경로는 파라미터가 아니라 **새 데이터**(틱 기반 봉내 가드 — PR#157 수집 중, 001210 −161k 방지류)와 **새 신호**(발굴 원천 개선 — 거래량 순위 외 소스). 표본이 작으므로(픽 33) 거래일이 쌓이면 시리즈 스크립트 재실행이 저비용 재검증 경로.
  - **어떻게 확인**: `uv run python scripts/diagnostics/entry_delay_study.py` 재실행.

- [x] **비대칭 출구 반사실 검증 — 기각 + 새 단서(진입 전 10분에 엣지)** (2026-07-04, diag)
  - **무엇**: `scripts/diagnostics/asymmetric_exit_study.py` — 실제 픽 33건(5/6~7/1, 분봉 09:10 진입 재현)에 "부분익절 도달 후 잔량만 완화" 가설을 검증. 잔량 트레일링 1.6/2.4/3.2%·15:15까지 보유·본전방어 + 대조군(전구간 완화 2.4%, buy-hold). **결과: 기각** — baseline(잔량도 0.8%)이 전 표본(−0.04%)과 부분익절 도달 8건(+2.44%) 모두 1위. 부분익절 후 가격은 대체로 되돌려서 좁은 트레일링이 고점 근처를 정확히 회수. 전구간 완화(−0.45%)·buy-hold(−0.24%)는 더 나쁨 — ATR 검증의 "넓히면 손해"가 5월 승자 포함 표본에서도 재확인.
  - **새 단서**: 봇 진입점(09:10)부터 재면 buy-hold 조차 −0.24%(비용 후)인데 선정 알파의 09:00 시가 기준은 +1.15% — **차이 ≈ 개장 후 첫 10분**. 픽의 오른꼬리 엣지 상당 부분이 진입 전에 지나감. 7/1 실측: 발굴 확정 09:01:50 → 첫 진입 09:09:58 (모멘텀 게이트가 15분 lookback history 를 기다리는 8분 공백).
  - **왜**: 2026-07-04 검증 시리즈 종결 — 출구 스택(손절·트레일링·부분익절 폭·비대칭)은 이 표본에서 사실상 로컬 최적으로 판명. 남은 개선 가설은 출구가 아니라 **진입 지연 단축**(발굴 직후 진입 시 모멘텀 게이트 8분 대기의 기회비용) 하나로 수렴.
  - **어떻게 확인**: `uv run python scripts/diagnostics/asymmetric_exit_study.py` 재실행.

- [x] **선정 알파 검증 — 실제 발굴 기록 기반 "거래량 상위 매수" 엣지 실측** (2026-07-04, diag)
  - **무엇**: `scripts/diagnostics/selection_alpha_study.py` — 실제 발굴 기록(리포트 rank_candidates/discovery_chosen, 21일 93행)의 당일 시가→종가를 KODEX 200 대비로 실측. ① **풀 자체는 독**: 후보 전체 초과수익 평균 −4.34%p(승률 31/92, 분봉 09:10→15:15 실측도 −6.07%p 로 확증) — "아침 거래량 상위"는 그날 종일 시장에 지는 집단. ② **게이트가 독을 거름**: 차단일 후보 −6.65%p vs 거래일 −2.31%p(breadth 게이트 재확증), **실제 픽은 +1.23%p(비용 후 +0.90%p)** — 시스템의 가치는 선정이 아니라 필터링. ③ **엣지는 오른꼬리에 있음**: 픽 절대수익 평균 +1.15% vs 중앙 −0.18%, 승률 14/33 — 소수 대박이 평균을 견인. 그런데 현행 출구(부분익절 +1.6% 절반 + 트레일링 0.8%)는 정확히 그 꼬리를 자르는 구조 → 원재료 플러스가 전략 손익 마이너스로 전환되는 메커니즘 규명. ④ 순위 정보력(1~3 vs 4~10)은 미미(−7.29 vs −8.67%p, 풀 기록 5일뿐).
  - **왜**: 출구·타이밍·쿨다운 검증(전부 무변경 결론) 후 남은 최종 질문. 결론 = 발굴/게이트는 유지, 문제는 "승자를 일찍 파는 출구". ATR 검증에서 '전면 넓히기'는 손해로 판명됐으므로, 다음 가설은 **비대칭 출구**(진입 직후엔 현행 좁게, 부분익절 도달 후 잔량만 트레일링 완화 — 꼬리 보존) 반사실.
  - **어떻게 확인**: `uv run python scripts/diagnostics/selection_alpha_study.py` 재실행. 표본 한계(픽 33행, 풀 기록 5일)는 docstring 명시.

- [x] **ATR 스케일 출구(트레일링) 반사실 검증 — 기각** (2026-07-04, diag)
  - **무엇**: 진입 축 검증의 후속 가설 "종목 변동성(ATR) 비례 출구 폭이 고정 0.8%보다 낫다"를 `scripts/diagnostics/atr_exit_study.py` 로 검증(같은 진입 11건, 출구 규칙만 교체 — 트레일링만/출구 3종 모두 × k 0.25~1.0 그리드). **결과: 기각.** 모든 변형이 고정 폭보다 나쁨(합계 −15k → −96k~−603k). 원인 = 전제 오류: 이 시장(연속 HIGH_VOL)에선 삼성전자조차 일봉 ATR 6.4~7.5%(일봉 원본으로 교차 확인 — 하루 진폭 6~10%)라 급등주(8~9.5%)와 차이가 없음. ATR 스케일링이 사실상 "전 종목 출구 넓히기"가 됐고 whipsaw 장에서 넓은 출구는 반납만 커짐. **현행 좁은 고정 폭이 테스트 규칙 중 최선 — 출구 파라미터 무변경.**
  - **왜**: "삼성전자 5왕복 4패"의 원인이 '저변동 대형주에 좁은 트레일링'이라는 가설이었는데, 실측하니 삼성전자도 고변동이라 가설 자체가 성립 안 함. 손실의 진짜 원인은 출구 폭이 아니라 선정/엣지 문제로 재수렴.
  - **어떻게 확인**: `uv run python scripts/diagnostics/atr_exit_study.py` 재실행. 재검토 조건: 시장 정상화로 종목 간 ATR 이 실제로 갈라질 때.
  - **남은 후보**: ① 선정 엣지(selection_study 확장 — "거래량 상위 매수"에 엣지가 있는가), ② 봉내 폭락 가드(틱 데이터 PR#157 축적 후 — 001210 −161k 는 출구 폭이 아니라 가드 granularity 문제).

- [x] **진입 축 3종 반사실 검증 — 쿨다운·개장 직후 진입·가격대 편향** (2026-07-04, diag)
  - **무엇**: 적대적 검토가 지목한 세 축을 `scripts/diagnostics/entry_axes_study.py`(read-only, 로그 원장 + ka10080 분봉 시뮬)로 검증. **표본 작음(4거래일 11왕복) — 방향 참고용.** ① 쿨다운 45~60 vs 90분: 90분 미만 재진입은 단 1건(6/16 삼성전자 +43,766원 승자)이었고 90분이었으면 +8,682원(지연) 또는 0(제거) — 짧은 쿨다운이 손해라는 증거 없음, 5/31 "승자 재진입 보존" 원칙과 일치 → **90분 강제 복원 근거 없음, 현행 유지**. ② 진입 지연(09:20~11:00 그리드): 어느 시각도 실제(재현 평균 -0.18%)보다 낫지 않음(-0.03%~-0.75%) → "개장 직후 진입이 문제" 가설 기각. 진짜 신호 = **모든 진입 시각에서 평균이 음수** — 문제는 타이밍이 아니라 손익 구조(트레일링 0.8% × 부분익절 절반 × 왕복비용 0.33%). ③ 가격대: "저가주 틱 노이즈" 가설 기각(저가주 틱/트레일링 8~13%로 오히려 미세). 실패 모드 2개 실측 — (a) 001210(1,472원) 봉내 폭락 1건이 -161,182원(분봉 가드 한계, 틱 로깅 PR#157 이 수집 중), (b) **삼성전자가 거래량 순위로 4일 연속 발굴돼 5왕복 4패 누적 -120,665원** — 저변동 대형주에 0.8% 트레일링은 구조적 whipsaw.
  - **왜**: 파라미터를 바꾸기 전에 반사실로 방향 확인(변경은 검증 후 원칙). 결과적으로 "무엇을 바꿀까"가 아니라 "바꾸지 말 것 2개(쿨다운·진입시각) + 진짜 문제 1개(종목 특성 무시한 고정 트레일링 폭)"로 좁혀짐.
  - **어떻게 확인**: `uv run python scripts/diagnostics/entry_axes_study.py` 재실행 — 원장·분봉 기반이라 결정론적(분봉 재조회 값 동일 가정).
  - **후속 후보**: 종목별 변동성(ATR) 대비 트레일링 폭 정규화 반사실 — `atr_normalized_weight`(PR39, 현재 off)와 별개로 *출구* 쪽 ATR 스케일링은 미구현. 틱 데이터가 쌓이면 봉내 가드 오버슈트 정량화와 함께 검토.

- [x] **레짐 라우팅 프로파일 동기화 — 회피 종목 합집합 병합 + defensive 한도 누락 보정** (2026-07-04)
  - **무엇**: 2026-07-04 적대적 검토에서 발견 — 레짐 라우터가 켜진 뒤(실측: 6/16 이후 전 운영일이 HIGH_VOL → daytrade-highvol.yaml) 실제 매매는 레짐 프로파일로 도는데, 주간 LLM 리뷰·수동 회피 지정은 `daytrade-current.yaml` 에만 반영돼 라우팅된 날 회피 목록(001470·004870)이 적용되지 않았다. ① paper-day 시동 시 라우팅이 일어나면 베이스(current)의 `avoid_symbols` 를 라우팅된 프로파일에 **합집합 병합**(`merge_base_avoid_symbols`) — 파일 간 복사 동기화 대신 단일 출처(current) 보존. ② `daytrade-defensive.yaml` 에 종목당 비중·전체 노출 한도 누락 보정(누락 시 엔진 기본 20%/80%가 적용돼 약세장 프로파일이 오히려 가장 느슨했음 → 10%/60% 명시). ③ 레짐 프로파일 4종 헤더에 "이 파일이 실제 적용본" 경고 + 사실과 어긋난 주석(top_n·임계 구값, "cooldown 불변" 오기) 교정.
  - **왜**: 사용자가 튜닝·주간 자동화하는 파일과 실제 적용 파일이 달라지는 구조적 드리프트 차단. cooldown 불일치(레짐 45~60 vs current 90)는 매매 행동을 바꾸므로 이번엔 주석으로만 표기 — ledger 반사실 검증 후 별도 결정.
  - **어떻게 확인**: `uv run pytest tests/test_daytrade_cli_helpers.py tests/test_daytrade_regime_router_integration.py -v` — 병합 3케이스 + 프로파일 한도 명시 회귀. 다음 paper-day 시동 콘솔에 "회피 종목 병합: … 2개(001470, 004870)" 라인 + 로그 `daytrade.avoid_symbols_merged`.

- [x] **틱 원본 JSONL 로깅 — 틱 기반 손절 가드 반사실 검증 데이터 수집** (2026-07-02)
  - **무엇**: 2026-07-01 금호전기(001210) 손절이 설정 -1%(`stop_loss_pct 0.010`)가 아닌 **-4.48%** 에서 체결(오버슈트 비용 ≈ 11.8만원, 그날 손실 17.2만원의 대부분). 원인 = `PositionGuard` 검사가 1분봉 확정 시점(`runner._on_bar` 끝)에만 돌아 봉 내부 급락을 놓침(09:09:58 매수 → 09:10:58 첫 봉 마감 검사 때 이미 -4.48%). 틱 기반 가드로 바꿀지 판단하려면 반사실 검증이 필요한데 틱이 어디에도 저장되지 않아 검증 불가 → **트레이딩 로직은 일절 안 건드리고** 러너가 이미 구독 중인 0B 웹소켓 틱을 일별 JSONL(`data/ticks/ticks-YYYY-MM-DD.jsonl`)로 read-only 저장하는 `monitor/tick_recorder.py` 추가. 핫패스 보호: 매 틱은 메모리 버퍼 append 만, 디스크 write 는 임계(200줄)/간격(5초) 도달 시 묶음 1회. `.env` 의 `TICK_LOG_DIR=data/ticks` 로 활성(비우면 비활성 — 기존 `LATENCY_LOG_PATH` 와 동일 opt-in 패턴).
  - **왜**: diag-first(inverse_etf_study 와 동일) — 데이터 먼저 모으고 "틱 기반 가드였다면 손절가가 얼마였나 / 승자를 조기 손절해 이익을 깎진 않았나"를 실측한 뒤에만 가드 로직 변경을 결정. 장중 3종목 × 6시간 수준이라 용량은 수 MB.
  - **어떻게 확인**: ① `.env` 에 `TICK_LOG_DIR=data/ticks` 추가 후 다음 paper-day 장중 `ls data/ticks/` → `ticks-YYYY-MM-DD.jsonl` 생성·증가. ② `tail -1 data/ticks/ticks-*.jsonl | python3 -m json.tool` → `ts/symbol/price/volume` 4필드. ③ `uv run pytest tests/test_monitor_tick_recorder.py -v` → 12 케이스 PASS.
  - **후속**: 틱이 수 거래일 쌓이면 반사실 분석(체결 틱 vs 봉 마감 검사 손절가 비교) 스크립트를 `scripts/diagnostics/` 에 추가 — 그 결과로 틱 기반 가드 전환 여부 결정.

- [x] **일일 손실 한도(churn 브레이크) 발동을 일일 리포트에 노출** (2026-07-02)
  - **무엇**: 2026-07-01 에 churn 브레이크가 10:27 발동(실현손실 202,646원 > 한도 100,000원)해 이후 매수 신호 252건을 종일 차단했는데, 일일 리포트엔 흔적이 전혀 없었다(`reentry_blocked_count` 는 매수여력 부족 거절만 세는 별개 필드). `runner.signal_blocked_daily_loss` 이벤트를 파서(`log_parser.py`)에 추가하고, `DailyReport` 에 `daily_loss_blocked_count`(차단 건수)·`daily_loss_blocked_first_time`(최초 발동 KST 시각) 신설. note_ko 에 "🛑 10:27 일일 손실 한도 도달 — 이후 신규 매수 중단 (신호 252건 차단)", timeline 에 최초 발동 1건(실현손실·한도 금액 포함) 표시.
  - **왜**: 발동 후엔 오후 내내 매수가 없는데 리포트만 보면 "왜 조용했는지" 알 수 없었다. 브레이크는 검증된 균형 가드라 로직은 무변경 — 가시성만 추가.
  - **어떻게 확인**: `uv run auto-trade report --date 2026-07-01 --out /tmp/x` 재생성 시 note_ko·timeline 에 🛑 라인, JSON 에 `daily_loss_blocked_count: 252`. `uv run pytest`(신규 회귀 3종 포함) 통과.

- [x] **거래불가 종목 사전 회피 + 발굴 보강** (2026-06-16)
  - **무엇**: 2026-06-16 로그에서 모의투자 매매제한 종목(004870 티웨이홀딩스)이 발굴에 채택돼 09:10~15:14 매분 주문 거부(RC4007)되며 에러 62건·슬롯 낭비. ① 거절 분류기(`reject_classifier`)에 `"매매제한"`·RC4007 패턴 추가 → `TRADING_HALT`(당일 skip)로 첫 거부 즉시 재시도 차단(62→1). ② 신규 `account/untradeable.py`(`UntradeableStore`, `data/state/untradeable.json`) — 러너가 종목 단위 거부(매매제한·거래정지·무효코드)를 디스크에 누적, within_days(기본 30일) 노화. ③ `top_volume(exclude_symbols=)` + `discover_and_arm` 가 후보를 top_n 보다 넉넉히(pool=max(top_n*3, top_n+7)) 받아 거래불가 이력·avoid·게이트 차단을 건너뛰고 다음 순위로 top_n 을 채움(슬롯 낭비 방지). `delayed_discover` 가 발굴 시 `active_symbols` 로드해 회피.
  - **왜**: 모의투자 매매제한은 실거래선 정상 거래되는 종목이라 시세/거래량 TR 응답에 사전 플래그가 없음 → "한 번 거부당하면 그날 차단 + 다음날 발굴 제외"가 유일한 견고한 해법. 라이브 러너의 universe 는 장 시작 후 변경 불가(set_universe 가드)라 장중 재발굴 대신 *발굴 시점 보강* 으로 설계(사용자 결정).
  - **어떻게 확인**: `uv run pytest`(신규 회귀 — 분류기 3·rank exclude·orchestrator top-up·untradeable store 7·runner 기록 2) 1236 통과. `uv run ruff check && uv run mypy src` 통과. 다음 paper-day 거부 발생 시 `data/state/untradeable.json` 생성 + 로그 `daytrade.untradeable_avoided`.
  - **한계/후속**: "오늘 처음" 거부당한 종목의 빈 슬롯은 그날은 못 메우고 다음날부터 자동 회피(장중 재발굴은 범위 밖). 별건으로 429 rate-limit 재시도 228건(ka20009 시세 폴링)·EOD 청산 리포트 sell_price null 정합성은 미해결.

- [x] **데이터 기반 전략 수립용 일일 로그 필드 확충** (2026-06-15, 3 PR 분할)
  - **무엇**: 일일 리포트가 "결과"만 남기고 "왜 그 결정을 했나=의사결정 맥락"이 빠져 매번 로그 재파싱이 필요하던 문제 해소. **Phase 1**([#143](https://github.com/Mizeloble/auto-stock-trading/pull/143)) 진입 사유·시각·매도 사유(guard 매칭)·왕복 손익(`round_trips`: 보유시간·손익·청산사유)·`regime`·`breadth_*`. **Phase 2**([#144](https://github.com/Mizeloble/auto-stock-trading/pull/144)) 신규 `reports/snapshot_reader.py` 로 topcap 스냅샷 사후 조인 → 진입 등락률·순위·MFE/MAE·차단 기회비용(`blocked_outcomes`). **Phase 3**(이 브랜치) `no_trade_reason`(미진입 사유) + 텔레그램 국면/시장폭 라인·미진입 사유 노출 + 주간 인사이트 LLM 입력에 승률·사유별 성과·국면·반사실 집계.
  - **왜**: `data/reports/*.json` 만 모으면 승률·사유별 성과·보유시간·게이트 기회비용을 바로 분석. 그간 손으로 하던 ledger 반사실 검증을 리포트에 자동 박제. 실데이터: 2026-06-09 삼성전자가 게이트 차단된 날 실제 +8.97%(기회비용 자동 기록).
  - **어떻게 확인**: `uv run auto-trade report --date 2026-05-26 --out /tmp/x` → round_trips·청산사유 분포 확인. `uv run pytest && uv run ruff check && uv run mypy src`.
  - **한계/후속**: topcap 스냅샷은 시총 상위만 담아 거래량 발굴 소형주는 진입 피처·MFE/MAE 가 None(대형주만 커버). 대시보드 daily.html 시각화는 데이터(asdict 자동 노출)만 되고 UI 미반영 — 선택 후속. 전체 커버는 `monitor/price_snapshot.py` 레코더가 발굴 종목까지 담아야 함.

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
