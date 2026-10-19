"""把 pydantic-ai 消息转换为展示条目。前端渲染和反思的文字记录共用这一份逻辑。"""

from collections.abc import Sequence
from typing import Any, Literal, TypedDict

from pydantic_ai.messages import (
    DocumentUrl,
    ImageUrl,
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

from muliao.files import parse_file_url

ItemType = Literal["user", "assistant", "thinking", "tool_call", "tool_result", "tool_error"]


class Attachment(TypedDict):
    # files.file_url：界面直接用它显示与下载
    url: str
    name: str
    media_type: str
    # 能内联显示的图片（files.INLINE_IMAGE_TYPES）；其余是下载链接
    inline: bool


class Item(TypedDict):
    type: ItemType
    text: str
    tool_name: str | None
    tool_call_id: str | None
    # 来源消息的数据库 id；由 API 填充，用于定位压缩点
    message_id: int | None
    # 用户消息的附件（见 runtime.attachments 的存储形态）；其他条目为空
    attachments: list[Attachment]


def _item(
    type_: ItemType,
    text: str,
    tool_name: str | None = None,
    tool_call_id: str | None = None,
    attachments: list[Attachment] | None = None,
) -> Item:
    return Item(
        type=type_,
        text=text,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        message_id=None,
        attachments=attachments or [],
    )


def _user_item(content: str | Sequence[Any]) -> Item:
    if isinstance(content, str):
        return _item("user", content)
    texts: list[str] = []
    attachments: list[Attachment] = []
    for c in content:
        if isinstance(c, str):
            texts.append(c)
        elif isinstance(c, ImageUrl | DocumentUrl):
            attachments.append(
                Attachment(
                    url=c.url,
                    name=parse_file_url(c.url)[1],
                    media_type=c.media_type,
                    inline=isinstance(c, ImageUrl),
                )
            )
        else:
            raise RuntimeError(f"stored user prompt holds unsupported content {type(c).__name__}")
    return _item("user", "\n".join(texts), attachments=attachments)


def to_items(messages: Sequence[ModelMessage]) -> list[Item]:
    items: list[Item] = []
    for msg in messages:
        if isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, UserPromptPart):
                    items.append(_user_item(part.content))
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
        files = "".join(f" [attachment {a['name']!r} ({a['media_type']})]" for a in item["attachments"])
        lines.append(f"[{label}{name}] {truncate(item['text'], max_item_chars)}{files}")
    return "\n".join(lines)
