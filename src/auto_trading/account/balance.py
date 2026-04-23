"""예수금·잔고 조회."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

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
    deposit: Decimal            # 예수금(사용 가능 현금)
    total_evaluation: Decimal   # 평가금액(보유종목 + 현금 등)
    total_pnl: Decimal
    holdings: list[Holding] = field(default_factory=list)


def _d(raw: object) -> Decimal:
    if raw is None:
        return Decimal(0)
    s = str(raw).replace(",", "").replace("+", "").strip()
    if s in ("", "-"):
        return Decimal(0)
    try:
        return Decimal(s)
    except Exception:
        return Decimal(0)


async def get_deposit(client: KiwoomRestClient) -> Decimal:
    data = await client.request(TR.DEPOSIT, PATH_ACCOUNT, {"qry_tp": "3"})
    for key in ("entr", "ord_alow_amt", "ord_alowa", "dnca_tot_amt"):
        if key in data:
            return _d(data[key])
    return Decimal(0)


async def get_balance(client: KiwoomRestClient) -> AccountSnapshot:
    """계좌평가잔고내역 (TR kt00018)."""
    body = {"qry_tp": "1", "dmst_stex_tp": "KRX"}
    data = await client.request(TR.BALANCE, PATH_ACCOUNT, body)

    holdings: list[Holding] = []
    rows = _find_rows(data)
    for row in rows:
        holdings.append(
            Holding(
                symbol=str(row.get("stk_cd") or row.get("pdno") or "").strip(),
                name=str(row.get("stk_nm") or row.get("prdt_name") or "").strip(),
                quantity=int(_d(row.get("rmnd_qty") or row.get("hldg_qty"))),
                avg_price=_d(row.get("pur_pric") or row.get("pchs_avg_pric")),
                current_price=_d(row.get("cur_prc") or row.get("prpr")),
                market_value=_d(row.get("evlt_amt") or row.get("evlu_amt")),
                pnl=_d(row.get("evltv_prft") or row.get("evlu_pfls_amt")),
                pnl_pct=_d(row.get("prft_rt") or row.get("evlu_pfls_rt")),
            )
        )

    deposit = _d(data.get("entr") or data.get("dnca_tot_amt"))
    if deposit == 0:
        try:
            deposit = await get_deposit(client)
        except Exception:
            pass

    return AccountSnapshot(
        deposit=deposit,
        total_evaluation=_d(
            data.get("tot_evlt_amt") or data.get("tot_evlu_amt") or data.get("prsm_dpst_aset_amt")
        ),
        total_pnl=_d(data.get("tot_evltv_prft") or data.get("evlu_pfls_smtl_amt")),
        holdings=holdings,
    )


def _find_rows(data: dict) -> list[dict]:
    for key, value in data.items():
        if key.startswith("_"):
            continue
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value
    return []
