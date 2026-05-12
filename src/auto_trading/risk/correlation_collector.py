"""상관계수 데이터 공급 — 29단계 PR28 (#15 Phase 2).

risk/correlation.py 가 *순수 계산* 만 책임지는 반면, 이 모듈은 키움 일봉 데이터를
가져와 페어 상관계수 dict 로 변환하는 *I/O 어댑터*. risk/ 규칙(외부 호출 금지) 을
지키기 위해 별도 모듈 — 매니저는 이 결과를 주입받기만 한다.

러너가 매일 한 번(또는 보유 변경 시) 호출 → 결과를 ``risk.evaluate`` 의
``holding_correlations`` 인자로 주입.
"""

from __future__ import annotations

import structlog

from ..kiwoom.rest import KiwoomRestClient
from ..market.chart import get_daily_bars
from .correlation import daily_returns, pairwise_pearson_correlation

__all__ = ["refresh_correlations"]

log = structlog.get_logger(__name__)


async def refresh_correlations(
    client: KiwoomRestClient,
    symbols: list[str],
    *,
    lookback_days: int = 30,
) -> dict[tuple[str, str], float]:
    """보유 종목 list → 페어 상관계수 dict.

    각 종목의 최근 ``lookback_days + 1`` 개 일별 종가를 fetch (한 페이지면 충분),
    수익률로 변환 후 ``pairwise_pearson_correlation`` 으로 페어 dict.

    실패 처리:
    - 한 종목 fetch 실패 → warning 로그 + 해당 종목 스킵 (나머지 페어는 정상 계산).
    - 페어가 하나도 안 만들어지면 빈 dict 반환 (호출자는 가드 자체를 스킵).
    """
    series: dict[str, list[float]] = {}
    for sym in symbols:
        try:
            bars = await get_daily_bars(client, sym, max_pages=1)
        except Exception as exc:
            log.warning("correlation.fetch_failed", symbol=sym, error=str(exc))
            continue
        if len(bars) < 2:
            continue
        # 키움 일봉은 최신 → 과거 순. 종가만 추출 후 lookback+1 개로 자르고 시간 정순으로 뒤집어
        # daily_returns(prices) 에 넘긴다.
        closes = [b.close for b in bars[: lookback_days + 1]]
        closes.reverse()
        rets = daily_returns(closes)
        if rets:
            series[sym] = rets
    return pairwise_pearson_correlation(series)
