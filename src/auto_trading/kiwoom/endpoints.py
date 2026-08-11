"""키움 REST API 엔드포인트 / TR ID 상수 모음.

키움 개발자 센터(https://openapi.kiwoom.com) 의 공식 문서를 참고해서 유지한다.
값이 바뀌면 여기만 고치면 되도록 문자열을 직접 하드코딩하지 말 것.

실제 TR 코드는 키움 문서에서 최종 확인 후 수정. 여기 값은 공식 가이드에서
자주 언급되는 코드를 기본으로 둔 것이며, 404/에러가 나면 문서와 대조해 교체한다.
"""

from __future__ import annotations

# --- URL 경로 ---
PATH_TOKEN = "/oauth2/token"
PATH_TOKEN_REVOKE = "/oauth2/revoke"

PATH_STKINFO = "/api/dostk/stkinfo"  # 종목 기본정보 (현재가·등락률·거래량 등)
PATH_MARKET = "/api/dostk/mrkcond"  # 시세 (호가창 등)
PATH_CHART = "/api/dostk/chart"  # 차트 (일/분봉)
PATH_ACCOUNT = "/api/dostk/acnt"  # 계좌 (예수금/잔고)
PATH_ORDER = "/api/dostk/ordr"  # 주문 (매수/매도/정정/취소)
PATH_RANK = "/api/dostk/rkinfo"  # 순위정보 (거래량 상위 등). probe 2026-05-03 확정.
PATH_SECTOR = "/api/dostk/sect"  # 업종/지수. probe 2026-05-03 — ka20009 가 이 path 응답.


# --- TR ID (api-id 헤더로 전송) ---
class TR:
    """문서상 자주 쓰이는 TR 코드. 사용 시점에 문서와 대조 후 확정."""

    # 시세
    STOCK_BASIC_INFO = "ka10001"  # 주식기본정보 (현재가/등락률/거래량 포함)
    QUOTE_CURRENT = "ka10004"  # 주식호가요청 (호가창 매수·매도 잔량 — 현재가 필드는 없음)

    # 차트
    CHART_DAILY = "ka10081"  # 주식일봉차트조회
    CHART_MINUTE = "ka10080"  # 주식분봉차트조회

    # 계좌
    DEPOSIT = "kt00001"  # 예수금상세현황
    BALANCE = "kt00018"  # 계좌평가잔고내역

    # 주문
    ORDER_BUY = "kt10000"  # 주식 매수주문
    ORDER_SELL = "kt10001"  # 주식 매도주문
    ORDER_MODIFY = "kt10002"  # 주식 정정주문
    ORDER_CANCEL = "kt10003"  # 주식 취소주문

    # 위험신호 (read-only 로깅용 — probe 2026-06-21 mock 실측 확정)
    VI_TRIGGERED = "ka10054"  # 변동성완화장치(VI) 발동종목 (path=stkinfo, 필수 12 — market/vi.py)

    # 순위 / 지수 (day-trade 발굴·게이트용 — probe 2026-05-03 확정)
    RANK_VOLUME = "ka10030"  # 당일 거래량 상위 (필수: crd_tp 등 — market/rank.py 참고)
    # 장중 투자자별 순매수 상위 — 발굴 대안 신호 A/B 연구용(probe 2026-08-09 mock 실측).
    # 필수: trde_tp / mrkt_tp / orgn_tp 3개뿐. 응답 행은 stk_cd/stk_nm/sel_qty/buy_qty/netslmt
    # (가격·누적거래량 필드 없음). ka90009·ka10034 는 시장 분리가 안 되거나 인버스 ETF 가
    # 상위를 점유해 후보에서 탈락 — 상세는 scripts/diagnostics/probe_rank_netbuy.py.
    RANK_INTRADAY_INVESTOR = "ka10065"
    # ka20006 은 "업종현재가" 라 일봉 미지원 — ka20009 "업종일봉조회" 가 정답.
    SECTOR_INDEX_DAILY = "ka20009"  # 업종 일봉 (KOSPI 종합 001 / KOSDAQ 종합 101)


# 업종(지수) 코드 — 키움 시장 코드 관습. probe 로 확인 후 필요시 보정.
SECTOR_CODE_KOSPI = "001"  # KOSPI 종합지수
SECTOR_CODE_KOSDAQ = "101"  # KOSDAQ 종합지수

# 거래량 순위(ka10030) 요청의 시장 구분(`mrkt_tp`) 값.
MARKET_TP_ALL = "000"
MARKET_TP_KOSPI = "001"
MARKET_TP_KOSDAQ = "101"

# 장중 투자자별 매매상위(ka10065) 의 투자자 구분(`orgn_tp`).
INVESTOR_TP_FOREIGN = "9000"  # 외국인
INVESTOR_TP_INSTITUTION = "9999"  # 기관계

# 업종 일봉(ka20009) 요청의 시장 구분(`mrkt_tp`) — 순위 TR 과 코드 체계 다름.
# probe 2026-05-03 로 ka20009 가 mrkt_tp 필수임이 확인됨.
INDEX_MRKT_TP_KOSPI = "0"
INDEX_MRKT_TP_KOSDAQ = "1"


__all__ = [
    "INDEX_MRKT_TP_KOSDAQ",
    "INDEX_MRKT_TP_KOSPI",
    "INVESTOR_TP_FOREIGN",
    "INVESTOR_TP_INSTITUTION",
    "MARKET_TP_ALL",
    "MARKET_TP_KOSDAQ",
    "MARKET_TP_KOSPI",
    "PATH_ACCOUNT",
    "PATH_CHART",
    "PATH_MARKET",
    "PATH_ORDER",
    "PATH_RANK",
    "PATH_SECTOR",
    "PATH_STKINFO",
    "PATH_TOKEN",
    "PATH_TOKEN_REVOKE",
    "SECTOR_CODE_KOSDAQ",
    "SECTOR_CODE_KOSPI",
    "TR",
]
