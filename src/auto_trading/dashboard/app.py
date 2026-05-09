"""FastAPI 앱 팩토리.

- ``create_app(reports_root, backtests_root)`` — CLI 가 경로 주입할 때.
- 모듈 레벨 ``app`` — `uvicorn auto_trading.dashboard.app:app` (cwd 기준, --reload 용).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import api, pages

_STATIC_DIR = Path(__file__).parent / "static"


def create_app(*, reports_root: Path, backtests_root: Path) -> FastAPI:
    instance = FastAPI(
        title="auto-trade 대시보드",
        description="투자 결과 분석 (로컬 전용 · 읽기만)",
        version="0.1.0",
    )
    instance.state.reports_root = reports_root.resolve()
    instance.state.backtests_root = backtests_root.resolve()
    instance.include_router(api.router)
    instance.include_router(pages.router)
    instance.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    return instance


app = create_app(
    reports_root=Path("data/reports"),
    backtests_root=Path("data/backtests"),
)

__all__ = ["app", "create_app"]
