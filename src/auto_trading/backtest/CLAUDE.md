# backtest/ 규칙

- **Look-ahead bias 금지**: 전략은 bar[i] 시점까지의 데이터만 볼 수 있고, 체결은 bar[i+1] 시가(또는 당일 종가) 에만 가능.
- 기본 수수료 0.015%, 거래세(매도) 0.20% (2026 인상 반영), 슬리피지 5bp(0.05%). 변경은 `broker.py` 상수에서만. 이 값은 `reports/daily.py`·`shadow/tracker.py` 와 단일 진실 출처로 맞춘다(한쪽만 바꾸면 손익 비교가 어긋남).
- 슬리피지는 고정 bp 외 `slippage_model` 선택(impact 모델) 도 있다 — 모델 선택도 `broker.py` 책임이며 다른 모듈에서 분기하지 말 것.
- 결과는 `data/backtests/{strategy}_{symbol}_{timestamp}/` 아래 `trades.csv` + `equity.csv` 로 저장.
- 지표(Sharpe/MDD 등) 계산 로직은 `metrics.py` 한 곳에서만 — 중복 구현 금지.
