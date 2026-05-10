"""anthropic Message → 강제된 `tool_use` 블록의 input dict 추출.

`tool_choice={"type": "tool", "name": <X>}` 로 강제 호출한 응답에서
해당 도구의 input dict 만 꺼내는 공통 헬퍼. SDK 응답 블록은 객체일 수도
dict 일 수도 있어 양쪽을 동일하게 다룬다.

호출자가 `pydantic.BaseModel.model_validate(...)` 등으로 후속 검증 — 헬퍼는
도메인 schema 를 모름 (proposal/insight/llm_advisor 가 각자 schema 보유).
"""

from __future__ import annotations

from typing import Any


def _block_attr(block: Any, key: str) -> Any:
    """객체 속성 또는 dict key — SDK 가 둘 중 어느 형태든 처리."""
    return getattr(block, key, None) or (block.get(key) if isinstance(block, dict) else None)


def extract_tool_use_input(response: Any, tool_name: str) -> dict[str, Any]:
    """응답 블록 중 첫 `tool_use` 일치 블록의 input dict 반환.

    `tool_name` 이 일치하지 않는 `tool_use` 는 스킵 (병렬 도구 사용 가능성 대비).
    input 이 dict 가 아니거나 일치 블록이 없으면 ValueError.
    """
    blocks = getattr(response, "content", None) or []
    for block in blocks:
        if _block_attr(block, "type") != "tool_use":
            continue
        if _block_attr(block, "name") != tool_name:
            continue
        tool_input = _block_attr(block, "input")
        if not isinstance(tool_input, dict):
            raise ValueError(f"{tool_name} tool_use 의 input 이 dict 가 아닙니다")
        return tool_input
    raise ValueError(f"응답에 {tool_name} tool_use 블록이 없습니다")
