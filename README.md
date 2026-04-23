# auto-stock-trading

키움증권 REST API를 이용한 Python 자동 주식 매매 봇. **모의투자(가상 돈) 기반으로 검증한 뒤에만 실거래로 넘어간다.**

## 설치

```bash
uv sync          # .venv 생성 + 의존성 설치
cp .env.example .env
# .env 파일을 열어 KIWOOM_APP_KEY / KIWOOM_APP_SECRET 에 모의투자 키를 입력
```

> 모의투자 AppKey 는 [키움 개발자 센터](https://openapi.kiwoom.com) 에서 모의투자 신청 후 발급.

## 실행 (구현되면서 하나씩 열린다)

```bash
uv run auto-trade quote 005930                                 # 현재가 조회
uv run auto-trade balance                                       # 모의 계좌 잔고
uv run auto-trade backtest --strategy sma_crossover --symbol 005930 --from 2024-01-01
uv run auto-trade paper    --strategy sma_crossover --symbols 005930
```

## 테스트

```bash
uv run pytest                       # 단위 테스트
uv run pytest -m integration        # 모의투자 실 API 호출 테스트 (.env 필요)
```

## 진행 상황

[todo-list.md](./todo-list.md) 에서 초보자 친화 설명과 함께 단계별 체크리스트 확인.
