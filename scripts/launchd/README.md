# launchd 자동 운영 가이드

집 맥북에서 day-trade 모드 자동 운영 + 시총 200 breadth 인프라 + 토요일 LLM 자동 갱신 + 매일 로그 정리.
`launchd-install.sh` 한 줄이 다음 **7개 잡** 을 등록하면 사이클 완성:

| 잡 | 트리거 | 동작 |
|---|---|---|
| `paper-day` | 평일 08:55 KST | 발굴 → 매수 → 15:15 자체 청산 → 종료. 일일 리포트 텔레그램 |
| `marketcap-universe` | 평일 08:50 KST | KRX 시총 상위 200 마스터 갱신 (breadth 게이트 입력) |
| `snapshot-top200` | 평일 09:00~15:30 10분 간격 | 시총 200 현재가 스냅샷 JSONL 누적 |
| `news-prefetch` | 토요일 06:00 KST | RSS 6개 소스 캐시 선반영 (LLM X · 비용 0) |
| `insight-weekly` | 토요일 09:00 KST | 인사이트 + weekly-proposal LLM (`daytrade-current.yaml` 자동 갱신) |
| `log-rotate` | 매일 02:30 KST | 30일↑ `*.log` → `.log.gz`, 90일↑ `.log.gz` 삭제, launchd-*.log 10MB↑ truncate |
| `heartbeat-check` | 평일 09:00~16:00 5분 간격 | 봇 heartbeat 누락 감지 → 텔레그램 알림 |

등록 후 상태 점검은 `uv run auto-trade ops` 한 줄로 8개 LaunchAgent(위 7개 + 만료된 1회성 잡)의 마지막 실행·종료 코드를 표로 봅니다.

로그: `logs/paper-day-YYYY-MM-DD.log` / `logs/insight-YYYY-MM-DD.log` / `logs/launchd-log-rotate.out.log`.

## 어떻게 등록

```bash
cd <저장소를 clone 한 경로>
chmod +x scripts/launchd-install.sh scripts/launchd-uninstall.sh
./scripts/launchd-install.sh
```

확인:
```bash
launchctl list | grep mizeloble.auto-trade
# com.mizeloble.auto-trade.paper-day        두 줄 보이면 등록 완료
# com.mizeloble.auto-trade.insight-weekly
```

## 어떻게 확인 (즉시 시동 테스트)

장 외 시간에 강제로 돌려 시동 흐름 확인:
```bash
launchctl kickstart -k gui/$UID/com.mizeloble.auto-trade.paper-day
# 텔레그램에 "▶️ paper 시작" 푸시 도착하면 시동 성공
# (장 외 시간이라 ws 는 안 붙고 시계 task 가 청산 시각까지 대기)

launchctl kickstart -k gui/$UID/com.mizeloble.auto-trade.insight-weekly
# data/reports/ 가 비어있으면 CLI 가 Exit(1) — paper-day 며칠 운영 후 의미 있음
```

## 사전 준비 (맥 설정)

| 항목 | 설정 | 이유 |
|---|---|---|
| 시스템 sleep | `caffeinate -i` 가 자동 차단 | 별도 작업 X |
| 디스플레이 sleep | 자도 됨 | 절전 |
| 뚜껑 | **열어두기** | 닫으면 clamshell sleep |
| 전원 어댑터 | 필수 | 6.5h 운영, 배터리 부족 |
| 자동 OS 업데이트 | 끄기 권장 | 한밤중 재부팅 시 등록 풀릴 수 있음 |

## 해제

```bash
./scripts/launchd-uninstall.sh
```

## 흔한 막힘

- **첫 등록 시 권한 팝업**: macOS 가 "백그라운드 항목"으로 등록 허용을 물어볼 수 있음 → 허용.
- **launchctl bootstrap 실패 (`Bootstrap failed: 5: Input/output error`)**: 같은 label 이 이미 등록돼 있음. install 스크립트가 자동 bootout 후 재등록하지만, 안 되면 수동으로:
  ```bash
  launchctl bootout gui/$UID/com.mizeloble.auto-trade.paper
  ```
- **plist 수정 후 반영 안 됨**: bootout 후 다시 install 스크립트.
- **저장소 plist 를 직접 bootstrap 하면 실패**: 저장소의 plist 는 경로 자리에 `__PROJECT_DIR__`
  플레이스홀더가 들어 있다(개인 경로 비공개 + 어느 위치에 clone 해도 동작). install 스크립트가
  복사하면서 실제 경로로 바꿔 주므로, 등록은 항상 `launchd-install.sh` 로 한다.
- **paper 프로세스가 launchd 자식이 아니라 파이프 후속으로 분리됨**: `paper-day-mode.sh` 가
  `exec` 로 caffeinate 를 띄우므로 정상은 launchd 의 직접 자식. 그래도 종료가 안
  잡히면 `pgrep -fa "auto-trade paper"` 로 PID 직접 확인.

## 운영 사양 변경

`scripts/paper-day-mode.sh` 의 `--top-n` / `--liquidate` 인자 한 줄만 수정. plist 재등록 X.
실주문 전환은 며칠 dry-run 검증 후 사용자 명시 승인이 있을 때만 `--no-dry-run` 으로.

종목 풀(거래량 상위 N) 외에 특정 종목으로 swing 운영(미리 정한 종목 보유)이 필요하면
`uv run auto-trade paper --profile strategies/profiles/auto_005930.yaml --dry-run` 수동 실행.
swing 자동화 (`paper.plist` / `paper-stop.plist`) 는 deprecated 처리되어 `scripts/legacy/`
로 이동했다 — 자세한 복귀 절차는 [`scripts/legacy/README.md`](../legacy/README.md).

---

## 22단계 · 주간 인사이트 (`insight-weekly` plist) — LLM 누적 분석

매주 토 09:00 KST 자동 실행. 직전 7일치 일일 리포트(`data/reports/daily-*.json`) 를
Claude API 로 누적 분석 → 텔레그램에 한국어 인사이트 푸시. 호출당 약 $0.05~0.10.

### 사전 준비

- `.env` 에 `ANTHROPIC_API_KEY` 채움 (없으면 CLI 가 Exit(1)).
- paper-day 가 며칠 운영돼 `data/reports/` 에 `daily-YYYY-MM-DD.json` 이 쌓여있어야 의미 있음.

### 등록·해제

```bash
# 등록 (plist 경로 치환이 필요하므로 install 스크립트 사용 — 다른 잡도 함께 재등록됨)
./scripts/launchd-install.sh

# 확인
launchctl list | grep mizeloble.auto-trade.insight-weekly

# 강제 즉시 시동 (검증용)
launchctl kickstart -k gui/$UID/com.mizeloble.auto-trade.insight-weekly

# 해제
launchctl bootout gui/$UID/com.mizeloble.auto-trade.insight-weekly
```

### 운영 사양 변경

`scripts/insight-weekly.sh` 의 `--days` 인자 한 줄만 수정.

### 수동 호출 (cron 외)

- CLI: `uv run auto-trade insight --days 7` (콘솔만), `--push` 추가 시 텔레그램.
- 텔레그램: 봇에 `/insight` 보내기 (5분 쿨다운). paper-day 운영 중일 때만 활성.
