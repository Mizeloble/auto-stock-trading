"""예수금·잔고 조회."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from decimal import Decimal

from .._common import extract_rows, normalize_symbol, to_decimal
from ..kiwoom.endpoints import PATH_ACCOUNT, TR
from ..kiwoom.rest import KiwoomRestClient


@dataclass(frozen=True)
class Holding:
    symbol: str
    name: str
    quantity: int
    avg_price: Decimal
    current_price: Decimal
    market_value: Decimal
    pnl: Decimal
    pnl_pct: Decimal


@dataclass(frozen=True)
class AccountSnapshot:
    deposit: Decimal  # 예수금(사용 가능 현금)
    total_evaluation: Decimal  # 평가금액(보유종목 + 현금 등)
    total_pnl: Decimal
    holdings: list[Holding] = field(default_factory=list)


async def get_deposit(client: KiwoomRestClient) -> Decimal:
    data = await client.request(TR.DEPOSIT, PATH_ACCOUNT, {"qry_tp": "3"})
    for key in ("entr", "ord_alow_amt", "ord_alowa", "dnca_tot_amt"):
        if key in data:
            return to_decimal(data[key])
    return Decimal(0)


async def get_balance(client: KiwoomRestClient) -> AccountSnapshot:
    """계좌평가잔고내역 (TR kt00018)."""
    body = {"qry_tp": "1", "dmst_stex_tp": "KRX"}
    data = await client.request(TR.BALANCE, PATH_ACCOUNT, body)

    holdings = [
        Holding(
            # 잔고 응답의 코드는 'A005930' / '252670_AL' 같은 변종이 섞여 있음.
            # 키움 주문 API 가 6자리만 받으므로 입력 단에서 정규화 (2026-05-08 청산
            # 거절 회귀 — 'A' prefix 가 그대로 들어가 liquidate_all 에서 거절).
            symbol=normalize_symbol(row.get("stk_cd") or row.get("pdno") or ""),
            name=str(row.get("stk_nm") or row.get("prdt_name") or "").strip(),
            quantity=int(to_decimal(row.get("rmnd_qty") or row.get("hldg_qty"))),
            avg_price=to_decimal(row.get("pur_pric") or row.get("pchs_avg_pric")),
            current_price=to_decimal(row.get("cur_prc") or row.get("prpr")),
            market_value=to_decimal(row.get("evlt_amt") or row.get("evlu_amt")),
            pnl=to_decimal(row.get("evltv_prft") or row.get("evlu_pfls_amt")),
            pnl_pct=to_decimal(row.get("prft_rt") or row.get("evlu_pfls_rt")),
        )
        for row in extract_rows(data)
    ]

    deposit = to_decimal(data.get("entr") or data.get("dnca_tot_amt"))
    if deposit == 0:
        with contextlib.suppress(Exception):
            deposit = await get_deposit(client)

    return AccountSnapshot(
        deposit=deposit,
        total_evaluation=to_decimal(
            data.get("tot_evlt_amt") or data.get("tot_evlu_amt") or data.get("prsm_dpst_aset_amt")
        ),
        total_pnl=to_decimal(data.get("tot_evltv_prft") or data.get("evlu_pfls_smtl_amt")),
        holdings=holdings,
    )
