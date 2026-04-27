# launchd 자동 운영 가이드

집 맥북에서 평일 장중에만 `auto-trade paper` 를 자동으로 돌리고, 회사에선
텔레그램으로 모니터링/제어하는 셋업.

## 무엇

- **08:55 KST**: launchd 가 `paper-day.sh` 트리거
  → `caffeinate -i uv run auto-trade paper --profile … --dry-run`
  → 텔레그램에 "▶️ paper 시작" 푸시
- **15:35 KST**: launchd 가 `paper-stop.sh` 트리거
  → `pkill -TERM` 으로 paper 프로세스 graceful shutdown
  → 텔레그램에 "⏹ paper 종료" 푸시
- **로그**: `logs/paper-YYYY-MM-DD.log` 에 일자별 누적

## 왜

회사에 있어 장중 직접 못 켤 때, 매일 자동으로 돌리고 폰에서 상태만 보면 됨.
`--dry-run` 모드라 실 주문 X — 전략·신호 흐름 검증 단계.

## 어떻게 등록

```bash
cd /Users/jjmize/Project/auto-stock-trading
chmod +x scripts/launchd-install.sh scripts/launchd-uninstall.sh
./scripts/launchd-install.sh
```

확인:
```bash
launchctl list | grep mizeloble.auto-trade
# com.mizeloble.auto-trade.paper       두 줄 보이면 등록 완료
# com.mizeloble.auto-trade.paper-stop
```

## 어떻게 확인 (즉시 시동 테스트)

장 외 시간에 한 번 강제로 돌려 동작 확인:
```bash
launchctl kickstart -k gui/$UID/com.mizeloble.auto-trade.paper
# 텔레그램에 "▶️ paper 시작" 푸시 도착하면 시동 성공
# (장 외 시간이라 ws 는 안 붙고 곧 종료됨 — 시동 자체가 성공이면 OK)

# 강제 종료:
launchctl kickstart -k gui/$UID/com.mizeloble.auto-trade.paper-stop
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
