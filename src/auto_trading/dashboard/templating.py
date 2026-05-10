"""Jinja2 템플릿 헬퍼 — 페이지 라우터에서 공유."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

__all__ = ["templates"]
