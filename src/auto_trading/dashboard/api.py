"""대시보드 JSON API 라우터.

읽기 전용. 응답에는 시크릿이 들어갈 수 없음 — 데이터원 자체가 공개 가능한 일일/백테스트 리포트.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from . import loaders

router = APIRouter(prefix="/api", tags=["dashboard"])


@router.get("/healthz")
def healthz() -> dict[str, bool]:
    return {"ok": True}


@router.get("/daily")
def list_daily(request: Request) -> dict[str, list[str]]:
    return {"dates": loaders.list_daily_dates(request.app.state.reports_root)}


@router.get("/daily/{date}")
def get_daily(date: str, request: Request) -> dict[str, Any]:
    payload = loaders.load_daily(request.app.state.reports_root, date)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"daily report not found: {date}")
    return payload


@router.get("/equity")
def get_equity(request: Request) -> list[dict[str, Any]]:
    return loaders.equity_series(request.app.state.reports_root)


@router.get("/backtests")
def list_backtests(request: Request) -> dict[str, list[dict[str, Any]]]:
    return {"runs": loaders.list_backtests(request.app.state.backtests_root)}


@router.get("/backtests/{run}")
def get_backtest(run: str, request: Request) -> dict[str, Any]:
    payload = loaders.load_backtest(request.app.state.backtests_root, run)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"backtest run not found: {run}")
    return payload
