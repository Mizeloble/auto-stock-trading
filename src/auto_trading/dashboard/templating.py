"""Jinja2 템플릿 헬퍼 — 페이지 라우터에서 공유."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def static_version(name: str) -> str:
    """`static/<name>` 의 수정시각(epoch 정수) — 캐시 버스팅 쿼리용.

    빌드 도구 없이(번들러 금지) `app.js` 가 바뀌면 URL 이 자동으로 달라져 브라우저가
    옛 JS 를 물고 있는 일을 막는다. 파일이 없으면 "0".
    """
    path = _STATIC_DIR / name
    try:
        return str(int(path.stat().st_mtime))
    except OSError:
        return "0"


# 템플릿에서 `{{ static_version('app.js') }}` 로 호출.
templates.env.globals["static_version"] = static_version

__all__ = ["static_version", "templates"]
