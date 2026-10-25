# scripts/legacy — deprecated 자동화

이 폴더의 파일은 **swing 모드 launchd 자동화 시절의 산출물**이다. 25단계 day-trade 전환 이후 `launchd-install.sh` 가 더 이상 등록하지 않으므로 평소에는 건드릴 일이 없다.

## 구성

| 파일 | 역할 |
|---|---|
| `paper-day.sh` | 평일 08:55 KST trigger — `auto-trade paper` (swing) 를 caffeinate -i 로 띄움 |
| `paper-stop.sh` | 평일 15:35 KST trigger — `pkill -TERM` 로 swing 러너 graceful shutdown |
| `launchd/com.mizeloble.auto-trade.paper.plist` | 위 `paper-day.sh` 를 호출하는 LaunchAgent |
| `launchd/com.mizeloble.auto-trade.paper-stop.plist` | 위 `paper-stop.sh` 를 호출하는 LaunchAgent |

## 현재 권장 흐름

day-trade 모드:

- 자동 등록: `./scripts/launchd-install.sh` → `paper-day` + `insight-weekly` + `log-rotate` 3종 등록
- 트리거 스크립트: `scripts/paper-day-mode.sh` (08:55 시동, 15:35 자동 종료까지 한 프로세스)
- plist: `scripts/launchd/com.mizeloble.auto-trade.paper-day.plist`

## swing 모드를 다시 쓰고 싶다면

수동 실행은 그대로 가능 (`auto-trade paper` CLI 는 코드에 그대로 살아있다):

```sh
uv run auto-trade paper --profile strategies/profiles/auto_005930.yaml --dry-run
```

자동 등록을 복귀하려면 두 plist 의 ProgramArguments 경로가 `scripts/legacy/paper-day.sh` / `scripts/legacy/paper-stop.sh` 로 갱신되어 있는지 확인 후 `launchctl bootstrap` 으로 직접 로드해야 한다 (`launchd-install.sh` 는 더 이상 처리하지 않는다). 이때 plist 의 `__PROJECT_DIR__` 플레이스홀더를 실제 저장소 경로로 바꾼 사본을 로드한다 — 예: `sed "s|__PROJECT_DIR__|$PWD|g" scripts/legacy/launchd/<파일>.plist > ~/Library/LaunchAgents/<파일>.plist`.

## 왜 삭제하지 않고 보존?

- swing → day-trade 전환 직후라 회귀 시 빠르게 복귀할 보험
- `auto-trade paper` 자체는 백테스트·수동 테스트에서 여전히 유용
- `launchd-uninstall.sh` 가 옛 라벨(`paper`, `paper-stop`)을 unload 처리하므로 사용자 시스템에 잔존 등록이 있을 수 있음 — 이 폴더가 있으면 수동 청소도 가능
