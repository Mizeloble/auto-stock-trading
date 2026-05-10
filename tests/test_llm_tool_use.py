"""extract_tool_use_input 단위 테스트.

SDK 응답 블록은 객체 / dict 두 형태 모두 가능 (anthropic SDK 버전·테스트 mock 차이).
헬퍼는 양쪽을 동일하게 다뤄야 한다.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from auto_trading.llm import extract_tool_use_input


def _resp(*blocks: object) -> SimpleNamespace:
    return SimpleNamespace(content=list(blocks))


def test_extracts_input_dict_from_object_block() -> None:
    """SDK 객체 블록 — getattr 경로."""
    block = SimpleNamespace(type="tool_use", name="propose_change", input={"weight": 0.5})
    assert extract_tool_use_input(_resp(block), "propose_change") == {"weight": 0.5}


def test_extracts_input_dict_from_dict_block() -> None:
    """dict 블록 — 테스트 mock 흔한 형태."""
    block = {"type": "tool_use", "name": "propose_insight", "input": {"summary_ko": "ok"}}
    assert extract_tool_use_input(_resp(block), "propose_insight") == {"summary_ko": "ok"}


def test_skips_non_tool_use_blocks_and_picks_matching_tool() -> None:
    """text 블록 + 다른 도구 tool_use + 일치 tool_use 가 섞여 있어도 일치만 반환."""
    text = SimpleNamespace(type="text", text="설명")
    other_tool = SimpleNamespace(type="tool_use", name="other_tool", input={"x": 1})
    target = SimpleNamespace(type="tool_use", name="propose_change", input={"weight": 0.7})
    assert extract_tool_use_input(_resp(text, other_tool, target), "propose_change") == {
        "weight": 0.7
    }


def test_raises_when_input_is_not_dict() -> None:
    """SDK 가 비정상 응답으로 input=None/list 등 보낼 때 ValueError."""
    block = SimpleNamespace(type="tool_use", name="propose_change", input="not-a-dict")
    with pytest.raises(ValueError, match="propose_change tool_use 의 input 이 dict 가 아닙니다"):
        extract_tool_use_input(_resp(block), "propose_change")


def test_raises_when_no_matching_tool_use_block() -> None:
    """tool_use 블록 없거나 모두 다른 tool_name 일 때 ValueError."""
    text_only = _resp(SimpleNamespace(type="text", text="설명만"))
    with pytest.raises(ValueError, match="응답에 propose_change tool_use 블록이 없습니다"):
        extract_tool_use_input(text_only, "propose_change")

    only_other = _resp(SimpleNamespace(type="tool_use", name="other", input={"x": 1}))
    with pytest.raises(ValueError, match="응답에 propose_change tool_use 블록이 없습니다"):
        extract_tool_use_input(only_other, "propose_change")


def test_handles_response_without_content_attribute() -> None:
    """응답이 content 속성을 갖지 않거나 None 일 때 ValueError (KeyError 등 노출 X)."""
    empty = SimpleNamespace()  # content 속성 없음
    with pytest.raises(ValueError, match="응답에 propose_change tool_use 블록이 없습니다"):
        extract_tool_use_input(empty, "propose_change")
