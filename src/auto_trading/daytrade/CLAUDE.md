# daytrade/ 규칙

- day-trade 모드의 **발굴·게이트·universe 주입 오케스트레이션 전용**. `orchestrator.discover_and_arm()` 가 거래량 상위 후보 → 시장지수 SMA 게이트 → 통과 종목을 `runner.set_universe()` 로 주입하고 `DiscoveryResult` 를 돌려준다.
- 이 모듈은 **타이밍 결정을 하지 않는다** — 언제 발굴/청산할지는 CLI(`paper-day`) 책임. 발굴(orchestrator)과 실행(runner)의 경계를 섞지 말 것.
- `profile.DaytradeProfile` 은 사용자가 **YAML 을 직접 편집**하는 스키마(`strategies/profiles/daytrade-*.yaml`). RiskLimits·params 필드 매핑이 여기 집중되므로 필드를 더할 때 load/dump 양쪽과 테스트(`test_daytrade_profile*`)를 함께 갱신한다(왜: 사용자가 코드 대신 이 YAML 로 전략을 짠다).
- `cli_helpers.paper_day_main()` 가 CLI→runner 조립 지점. CLI 는 이것을 호출만 하고 조립 로직을 복제하지 않는다.
