# launchd 자동 운영 가이드

집 맥북에서 day-trade 모드 자동 운영 + 토요일 LLM 자동 갱신.
21~24단계 풀 도입 후 **두 잡** 만 등록하면 사이클 완성:

| 잡 | 트리거 | 동작 |
|---|---|---|
| `paper-day` | 평일 08:55 KST | 발굴 → 매수 → 15:15 자체 청산 → 종료. 일일 리포트 텔레그램 |
| `insight-weekly` | 토요일 09:00 KST | 인사이트 + weekly-proposal LLM (`daytrade-current.yaml` 자동 갱신) |

로그: `logs/paper-day-YYYY-MM-DD.log` / `logs/insight-YYYY-MM-DD.log`.

## 어떻게 등록

```bash
cd /Users/jjmize/Project/auto-stock-trading
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
- **paper 프로세스가 launchd 자식이 아니라 파이프 후속으로 분리됨**: `paper-day.sh` 가
  `exec` 로 caffeinate 를 띄우므로 정상은 launchd 의 직접 자식. 그래도 stop 이 안
  잡으면 `pgrep -fa "auto-trade paper"` 로 PID 직접 확인.

## YAML 프로파일 변경 시

`strategies/profiles/auto_005930.yaml` 의 숫자만 바꿔서 다음 평일 시동 때 자동 반영.
스크립트·plist 재등록 X. 다른 종목 추가하려면 새 profile 만든 뒤 paper-day.sh 의
`--profile` 인자 수정.

---

## 21단계 · day-trade 모드 (`paper-day` plist) — 별도 운영

기존 swing 모드(위) 와 별도로 day-trade 운영을 원할 때 사용. 둘 중 **하나만** 활성화하세요.

### 차이점 한눈에

| 항목 | swing (`paper.plist`) | day-trade (`paper-day.plist`) |
|---|---|---|
| 종목 | YAML 프로파일에 미리 명시 | 매일 09:00 거래량 상위 N 자동 발굴 |
| 매수 | 전략 신호 발생 시 | 발굴 직후 즉시 균등 매수 (weight=1/N) |
| 청산 | 외부 stop 시각 (15:35 SIGTERM) | 러너 자체 시계 (15:15 시장가 일괄 매도 → 자기 종료) |
| stop plist | 별도 필요 | **불필요** (자기 종료) |
| 다음날 갭 리스크 | 있음 (포지션 보유) | 없음 (당일 청산) |

### 등록·전환

```bash
# swing → day-trade 전환
./scripts/launchd-uninstall.sh                # swing plist 두 개 제거
launchctl bootstrap gui/$UID scripts/launchd/com.mizeloble.auto-trade.paper-day.plist

# 확인
launchctl list | grep mizeloble.auto-trade
# com.mizeloble.auto-trade.paper-day  한 줄 보이면 등록 완료

# 강제 즉시 시동 (장 외 시간 동작 검증)
launchctl kickstart -k gui/$UID/com.mizeloble.auto-trade.paper-day

# day-trade → swing 복귀
launchctl bootout gui/$UID/com.mizeloble.auto-trade.paper-day
./scripts/launchd-install.sh
```

### 운영 사양 변경

`scripts/paper-day-mode.sh` 의 `--top-n` / `--liquidate` 인자 한 줄만 수정. plist 재등록 X.
실주문 전환은 며칠 dry-run 검증 후 사용자 명시 승인이 있을 때만 `--no-dry-run` 으로.

---

## 22단계 · 주간 인사이트 (`insight-weekly` plist) — LLM 누적 분석

매주 토 09:00 KST 자동 실행. 직전 7일치 일일 리포트(`data/reports/daily-*.json`) 를
Claude API 로 누적 분석 → 텔레그램에 한국어 인사이트 푸시. 호출당 약 $0.05~0.10.

### 사전 준비

- `.env` 에 `ANTHROPIC_API_KEY` 채움 (없으면 CLI 가 Exit(1)).
- paper-day 가 며칠 운영돼 `data/reports/` 에 `daily-YYYY-MM-DD.json` 이 쌓여있어야 의미 있음.

### 등록·해제

```bash
# 등록
launchctl bootstrap gui/$UID scripts/launchd/com.mizeloble.auto-trade.insight-weekly.plist

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
