# backtest/ 규칙

- **Look-ahead bias 금지**: 전략은 bar[i] 시점까지의 데이터만 볼 수 있고, 체결은 bar[i+1] 시가(또는 당일 종가) 에만 가능.
- 기본 수수료 0.015%, 거래세(매도) 0.20% (2026 인상 반영), 슬리피지 5bp(0.05%). 변경은 `broker.py` 상수에서만.
- 결과는 `data/backtests/{strategy}_{symbol}_{timestamp}/` 아래 `trades.csv` + `equity.csv` 로 저장.
- 지표(Sharpe/MDD 등) 계산 로직은 `metrics.py` 한 곳에서만 — 중복 구현 금지.
