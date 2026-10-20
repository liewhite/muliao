"""把 pydantic-ai 消息转换为展示条目。前端渲染和反思的文字记录共用这一份逻辑。

消息上的约定（metadata 键、标记、合成结果）登记在 muliao.conventions，这里只读它们来展示。
"""

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

from muliao.conventions import (
    RUN_STOPPED,
    Attachment,
    event_indices,
    parse_event,
    tool_attachments,
    tool_threads,
)
from muliao.files import parse_file_url

ItemType = Literal[
    "user", "event", "assistant", "thinking", "tool_call", "tool_result", "tool_error", "stopped"
]


class Item(TypedDict):
    type: ItemType
    text: str
    tool_name: str | None
    tool_call_id: str | None
    # 来源消息的数据库 id；由 API 填充，用于定位压缩点
    message_id: int | None
    # 会话事件（type 为 event）：tool_name 是事件的来源（如 job:12），text 是事件内容
    # 用户消息的附件（见 runtime.attachments 的存储形态），或工具结果挂上的文件（TOOL_ATTACHMENTS）；其他条目为空
    attachments: list[Attachment]
    # 工具结果关联的子会话 id（TOOL_THREADS）；其他条目为空
    threads: list[str]


def _item(
    type_: ItemType,
    text: str,
    tool_name: str | None = None,
    tool_call_id: str | None = None,
    attachments: list[Attachment] | None = None,
    threads: list[str] | None = None,
) -> Item:
    return Item(
        type=type_,
        text=text,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        message_id=None,
        attachments=attachments or [],
        threads=threads or [],
    )


def _prompt_items(content: str | Sequence[Any], events: Sequence[int]) -> list[Item]:
    """一条用户消息展示成的条目：会话事件（RUN_EVENTS 标出的内容段）各自成条，其余连续的内容合成一条用户消息。"""
    if not events:
        return [_user_item(content)]
    if isinstance(content, str):
        raise RuntimeError("a user prompt marked with events must be a content list")
    items: list[Item] = []
    own: list[Any] = []
    for i, c in enumerate(content):
        if i in events:
            if own:
                items.append(_user_item(own))
                own = []
            source, text = parse_event(c)
            items.append(_item("event", text, tool_name=source))
        else:
            own.append(c)
    if own:
        items.append(_user_item(own))
    return items


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
                    items.extend(_prompt_items(part.content, event_indices(msg)))
                elif isinstance(part, ToolReturnPart):
                    items.append(
                        _item(
                            "tool_result",
                            part.model_response_str(),
                            part.tool_name,
                            part.tool_call_id,
                            tool_attachments(part),
                            tool_threads(part),
                        )
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
        if msg.metadata is not None and msg.metadata.get(RUN_STOPPED):
            items.append(_item("stopped", ""))
    return items


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}…[truncated {len(text) - limit} chars]"


def to_text(items: Sequence[Item], *, max_item_chars: int) -> str:
    labels = {
        "user": "USER",
        "event": "EVENT",
        "assistant": "ASSISTANT",
        "thinking": None,
        "tool_call": "TOOL CALL",
        "tool_result": "TOOL RESULT",
        "tool_error": "TOOL ERROR",
        "stopped": "STOPPED BY USER",
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
