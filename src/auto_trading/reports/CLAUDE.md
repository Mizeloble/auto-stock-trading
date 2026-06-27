# reports/ 규칙

- 리포트는 **사후 집계만** — 러너의 트레이딩 핫패스에 영향 X. 모든 함수는 입력(snapshot/log path) 만 보고 결정론적으로 결과를 만든다.
- 로그 파서는 JSON 라인만 파싱. 비-JSON 라인(rich 콘솔 출력 등) 은 조용히 skip — 운영 중 stderr 가 섞여도 깨지지 않게.
- 외부 호출 금지. 키움 REST/WS/텔레그램 호출은 호출자(러너·CLI)가 책임.
- 직렬화는 JSON. `data/reports/daily-YYYY-MM-DD.json` 으로 저장 → Phase B(주간 인사이트) 가 직전 N일치 입력으로 사용.
- **`daily/` 패키지 구조**: 원래 단일 `daily.py` 를 책임별 서브모듈로 분리. 공개 API 경로(`auto_trading.reports.daily.X`)는 `daily/__init__.py` 재노출로 그대로 유지된다 — import 변경 불필요. `models`(dataclass) / `_util`(변환·포맷) / `extract`(로그→레코드) / `aggregate`(비용·손익·COST_*) / `timeline` / `render`(note_ko·종목명) / `persist`(JSON I/O) / `builder`(오케스트레이션).
- 로그 파싱은 `log_parser.py` 의 `LogEvents` **한 곳**. 신규 이벤트 필드를 더하면 여기와 `daily/extract.py` 집계를 함께 갱신한다(중복 파서 만들지 말 것).
- **비용 상수 동기화(중요)**: `daily/aggregate.py` 의 `COST_COMMISSION_RATE`/`COST_TAX_RATE_SELL`/`COST_SLIPPAGE_BPS` 는 `backtest/broker.py` 기본값과 수동으로 일치시킨다. import 로 안 묶는 이유는 reports/ 외부 의존 최소화 — 한쪽만 바꾸면 리포트 손익이 백테스트와 어긋난다.
- **직렬화 footgun(중요)**: `DailyReport`(`daily/models.py`) 에 필드를 더하면 저장은 `asdict` 로 자동이지만 복원 `daily/persist.py` 의 `_from_json_dict` 는 **수동**이라 양쪽을 함께 고쳐야 한다(과거 `turnover_ratio` 가 복원 누락으로 항상 None 이던 전례). `test_save_load_roundtrip_preserves_every_field` 가 회귀를 잡는다 — 신규 필드는 그 테스트에도 비-기본값으로 넣을 것.
- **데이터 기반 분석 필드(2026-06)**: `daily/` 빌더가 로그+스냅샷을 사후 조인해 의사결정 맥락을 박제한다 — `TradeRecord.reason/time_kst/change_pct_at_entry/rank_at_entry`, `RoundTrip`(왕복 손익·보유시간·청산사유·MFE/MAE), `DailyReport.regime/breadth_*/blocked_outcomes/no_trade_reason`. 진입 사유·regime 은 소스 로그(`runner`·`daytrade.regime`)에 의존해 *그 변경 이후* 로그부터 채워짐. 나머지는 과거 로그도 소급 복원.
- **스냅샷 조인은 `snapshot_reader.py`**: `data/snapshots/topcap-<date>.jsonl` read-only 리더(log_parser 와 같은 정신 — 손상 라인 skip, 외부 호출 없음, 파일 없으면 빈 리더). ⚠️ topcap 은 **시총 상위만** 담아 거래량 발굴 소형주는 진입 피처·MFE/MAE 가 None(대형주만 커버) — 자세한 건 메모리 `project_snapshot_join_coverage`.
