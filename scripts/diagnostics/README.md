# scripts/diagnostics — 키움 API raw 응답 캡처

운영용이 아니라 **개발자 진단 도구**. `KiwoomRestClient` 가 `return_code ≠ 0` 면 `KiwoomError` 로 raise 하므로 응답 본문 자체를 보고 싶을 때 쓴다.

## 도구

| 파일 | 용도 |
|---|---|
| `probe_orders.py` | 모의 주문 응답 dict 캡처 — `ord_no`/`odno` 등 주문번호 필드명 확인 (`account/orders.py` 매핑 검증) |
| `probe_rank_index.py` | 거래량 순위 + 업종 일봉 응답 dict 캡처 — `TR.RANK_VOLUME` / `TR.SECTOR_INDEX_DAILY` 필드명 확인 (`market/rank.py`, `market/index.py` 후보 체인 보정) |
| `selection_study.py` | **선별 효과 읽기전용 검증** — "거래량만 vs 거래량+전날추세 재정렬"이 고른 종목의 당일 시가→종가 수익률을 개선하는지 12개월 일봉으로 측정(gate 아님 ranking, 거래수 불변). baseline/improved/pool 평균수익·승률·페어드 t 출력 + `data/studies/*.json` 저장. ⚠️ 시가→종가 근사·비용 제외·고정 바스켓·생존편향·소표본 → 방향성 신호일 뿐 증명 아님. |
| `alt_rank_ab_study.py` | **발굴 A/B 판정** — alt-rank.jsonl(PR #164)의 거래량 풀 vs 거래대금 풀을 자체 스냅샷(09:10→15:15)과 조인해 원재료 수익률 비교. API 호출 없음. 거래일 15일 미만이면 "판정 보류". |
| `shadow_gate_study.py` | **섀도우 vs 게이트 판정** — 차단일 가상 매매 실적(shadow-*.json, PR #172)을 집계해 SMA 게이트 완화 가설 판정 재료 생성. buy-hold 대비 출구 효과 병기. `--selftest` 로 데이터 없이 로직 검증. |
| `tick_guard_study.py` | **틱 가드 반사실 판정** — 틱 로그(PR #157) 위에서 실제 왕복의 출구만 틱 단위 가드로 교체 시뮬(규칙·순서 PositionGuard 동일, 파라미터 highvol yaml). 봉 마감 검사 대비 Δ손익·손절 오버슈트 산출. `--selftest` 지원. |
| `reentry_exposure_study.py` | **재매수 허용 반사실** — daily_notional_limit 를 누적→미청산 기준(매도분만큼 재매수 허용)으로 바꿨다면의 손익. 로그의 매수여력 거절 신호(쿨다운·횟수 체크 통과분)를 시각 순 재생, 분봉+동일 출구 스택 시뮬. 2026-07-16 판정: 4일 9진입 전패 Δ−285k — 기각, 누적 방식 유지. |
| `etf_pool_study.py` | **ETF 풀 편입 반사실** — 초고유동성 ETF 7종(레버리지·인버스·대표지수)에 현행 전략 규칙(모멘텀 0.35% 진입 + 출구 스택) 그대로 적용, 주식/ETF(매도세 면제) 비용 병기 + 봇 거래일 head-to-head. 2026-07-19 판정: 224거래 전 종목 음(−), 평균 −0.37%·승률 29%, 매일 모멘텀 통과라 풀·예산 도배 위험 — **기각, exclude_etf=True 유지**. |
| `snapshot_selection_study.py` | **자체 10분 스냅샷 선별 축 판정(선작성)** — 발굴 풀을 4가지 순위(현행 거래량 / 개장 10분 거래대금 / 전일 오후 추세 / 전일 갭)로 top3 뽑아 원재료 수익률 비교. API 호출 없음(alt-rank + topcap/discovered 스냅샷). 평가 가능일 15일 미만이면 "판정 보류". 휴장일(예: 2026-07-17, 전 종목 0% 고정) 자동 배제. `--selftest` 지원. |
| `position_weight_study.py` | **종목당 비중 그리드 반사실** — max_position_weight 4~20% 로 highvol 시대 전 거래를 재사이징 재생(일한도 10M·브레이크 −10만·쿨다운 유지, 작은 비중은 예산 잔여로 거절 신호 추가 진입까지 반영). w=7% 재생이 리포트 PnL 을 8/8일 재현. 2026-07-16 판정: 비중은 손익 스케일·분산만 바꿈(엣지 불변), 상향은 손실 확대 — 현행 7% 유지. |

## 실행

```sh
# 환경: KIWOOM_APP_KEY / KIWOOM_APP_SECRET 가 .env 또는 환경변수에 있어야 함
# probe_rank_index.py 는 KIWOOM_MODE=mock 만 동작 (실거래 차단)

uv run python scripts/diagnostics/probe_orders.py
uv run python scripts/diagnostics/probe_rank_index.py
```

응답에 포함된 시크릿(`secret`, `token`, `key` 키)은 자동 마스킹된다 (`_redact()`).

## 언제 다시 쓰나

- 키움이 응답 스키마를 바꿔 단위 테스트가 깨졌는데 디버그가 어려울 때
- 새 TR ID 를 추가하기 전에 실제 응답 형태를 확인할 때
- 주문 거절 사유 (예: A prefix · `_AL` suffix) 같은 응답 패턴을 실측으로 잡을 때

상세 사용 맥락은 `docs/history.md` (또는 `todo-list.md`) 의 21단계 (probe_orders), 22단계 (probe_rank_index) 항목 참고.
