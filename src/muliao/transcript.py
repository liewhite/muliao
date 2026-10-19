"""把 pydantic-ai 消息转换为展示条目。前端渲染和反思的文字记录共用这一份逻辑。"""

from collections.abc import Sequence
from typing import Any, Literal, TypedDict

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

ItemType = Literal["user", "assistant", "thinking", "tool_call", "tool_result", "tool_error"]


class Item(TypedDict):
    type: ItemType
    text: str
    tool_name: str | None
    tool_call_id: str | None


def _item(type_: ItemType, text: str, tool_name: str | None = None, tool_call_id: str | None = None) -> Item:
    return Item(type=type_, text=text, tool_name=tool_name, tool_call_id=tool_call_id)


def _user_text(content: str | Sequence[Any]) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(c if isinstance(c, str) else f"[{type(c).__name__}]" for c in content)


def to_items(messages: Sequence[ModelMessage]) -> list[Item]:
    items: list[Item] = []
    for msg in messages:
        if isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, UserPromptPart):
                    items.append(_item("user", _user_text(part.content)))
                elif isinstance(part, ToolReturnPart):
                    items.append(
                        _item("tool_result", part.model_response_str(), part.tool_name, part.tool_call_id)
                    )
                elif isinstance(part, RetryPromptPart):
                    items.append(
                        _item("tool_error", part.model_response(), part.tool_name, part.tool_call_id)
                    )
        elif isinstance(msg, ModelResponse):
            for part in msg.parts:
                if isinstance(part, TextPart) and part.content:
                    items.append(_item("assistant", part.content))
                elif isinstance(part, ThinkingPart) and part.content:
                    items.append(_item("thinking", part.content))
                elif isinstance(part, ToolCallPart):
                    items.append(
                        _item("tool_call", part.args_as_json_str(), part.tool_name, part.tool_call_id)
                    )
    return items


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}…[truncated {len(text) - limit} chars]"


def to_text(items: Sequence[Item], *, max_item_chars: int) -> str:
    labels = {
        "user": "USER",
        "assistant": "ASSISTANT",
        "thinking": None,
        "tool_call": "TOOL CALL",
        "tool_result": "TOOL RESULT",
        "tool_error": "TOOL ERROR",
    }
    lines = []
    for item in items:
        label = labels[item["type"]]
        if label is None:
            continue
        name = f" {item['tool_name']}" if item["tool_name"] else ""
        lines.append(f"[{label}{name}] {truncate(item['text'], max_item_chars)}")
    return "\n".join(lines)
