"""투자 결과 분석 대시보드 (FastAPI 로컬 전용).

엔트리는 ``auto_trading.dashboard.app:create_app`` (CLI 인자 주입) 또는
``auto_trading.dashboard.app:app`` (cwd 기준 기본 인스턴스 — uvicorn --reload 용).
"""

from .app import app, create_app

__all__ = ["app", "create_app"]
