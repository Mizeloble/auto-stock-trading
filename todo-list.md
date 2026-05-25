# 할 일 체크리스트 (초보자 가이드)

각 항목은 **무엇을 만드는지 / 왜 필요한지 / 어떻게 확인하는지** 세 줄로 설명합니다.

---

> 완료된 1~29단계 PR 이력(머지 PR 번호·날짜 포함)은 [docs/history.md](docs/history.md) 로 이관. 여기에는 **미완 작업 + 선택 과제만** 남긴다.

## 🔧 진행 중 — 미완 항목

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
