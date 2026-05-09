"""대시보드 HTML 페이지 라우터 (Jinja2)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .templating import templates

router = APIRouter(tags=["dashboard-pages"])


@router.get("/", response_class=HTMLResponse)
def overview(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "overview.html", {"page": "overview"})


@router.get("/daily", response_class=HTMLResponse)
def daily_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "daily.html", {"page": "daily"})


@router.get("/backtests", response_class=HTMLResponse)
def backtests_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "backtests.html", {"page": "backtests"})
