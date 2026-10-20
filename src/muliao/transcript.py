"""把 pydantic-ai 消息转换为展示条目（Item）：会话页的历史、运行中的实时条目（runtime.runner）、反思与压缩的文字记录
共用这一份投影，前端只渲染与合并，不再自己投影。

消息上的约定（metadata 键、标记、合成结果）登记在 muliao.conventions，这里只读它们来展示。
"""

from collections.abc import Collection, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ValidationError
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
from muliao.runtime.questions import ASK_USER, AnsweredQuestion, AskResult, Question, questions_of

ItemType = Literal[
    "user", "event", "assistant", "thinking", "tool_call", "tool_result", "tool_error", "stopped"
]


class AskView(BaseModel):
    """ask_user 调用的状态，由后端按唯一规则给出（annotate_asks），前端只渲染：
    asking 运行还没停下（还不能回答）；pending 现在能回答（显示表单）；submitted 回答已提交、还排着；answered 已回答；
    not_answered 用户没回答（发了新消息或只回答了别的提问）。"""

    status: Literal["asking", "pending", "submitted", "answered", "not_answered"]
    questions: list[Question]
    # answered 时每题的回答
    answers: list[AnsweredQuestion]
    # not_answered 时交给模型的说明
    note: str | None


class Item(BaseModel):
    type: ItemType
    text: str
    # 会话事件（type 为 event）：tool_name 是事件的来源（如 job:12），text 是事件内容
    tool_name: str | None
    tool_call_id: str | None
    # 来源消息的数据库 id；会话接口填充，用于定位压缩点；实时条目为 None
    message_id: int | None
    # 用户消息的附件（见 runtime.attachments 的存储形态），或工具结果挂上的文件（TOOL_ATTACHMENTS）；其他条目为空
    attachments: list[Attachment]
    # 工具结果关联的子会话 id（TOOL_THREADS）；其他条目为空
    threads: list[str]
    # ask_user 的调用（参数合法时）；其他条目为 None
    ask: AskView | None


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
        ask=None,
    )


def call_item(part: ToolCallPart) -> Item:
    item = _item("tool_call", part.args_as_json_str(), part.tool_name, part.tool_call_id)
    if part.tool_name == ASK_USER:
        try:
            questions = questions_of(part)
        except (ValidationError, KeyError):
            # 参数被工具 schema 拒绝、交还模型重试的提问（它有 tool_error 结果）：按普通工具调用展示
            return item
        item.ask = AskView(status="asking", questions=questions, answers=[], note=None)
    return item


def annotate_asks(items: list[Item], answerable: Collection[str], submitted: Collection[str]) -> list[Item]:
    """给 ask_user 调用填上状态，唯一规则：
    - 被退回（结果是 tool_error，如这次运行不提供提问）：不是提问，按普通工具调用加错误展示（ask 为 None）；
    - 有结果的按结果（AskResult）：answered / not_answered；
    - 没有结果的：现在能回答（answerable）是 pending，回答已提交、还排着（submitted）是 submitted，否则运行还没停下（asking）。"""
    results = {
        i.tool_call_id: i
        for i in items
        if i.tool_name == ASK_USER and i.type in ("tool_result", "tool_error")
    }
    return [_annotated(item, results.get(item.tool_call_id), answerable, submitted) for item in items]


def _annotated(
    item: Item, result: Item | None, answerable: Collection[str], submitted: Collection[str]
) -> Item:
    if item.ask is None:
        return item
    if result is not None and result.type == "tool_error":
        return item.model_copy(update={"ask": None})
    if result is not None:
        parsed = AskResult.model_validate_json(result.text)
        update = {
            "status": "answered" if parsed.answered else "not_answered",
            "answers": parsed.answers,
            "note": parsed.note,
        }
    elif item.tool_call_id in answerable:
        update = {"status": "pending"}
    elif item.tool_call_id in submitted:
        update = {"status": "submitted"}
    else:
        return item
    return item.model_copy(update={"ask": item.ask.model_copy(update=update)})


def result_item(part: ToolReturnPart | RetryPromptPart) -> Item:
    """工具结果，或参数被拒绝、交还模型重试的结果。"""
    if isinstance(part, ToolReturnPart):
        return _item(
            "tool_result",
            part.model_response_str(),
            part.tool_name,
            part.tool_call_id,
            tool_attachments(part),
            tool_threads(part),
        )
    return _item("tool_error", part.model_response(), part.tool_name, part.tool_call_id)


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
                elif isinstance(part, ToolReturnPart | RetryPromptPart):
                    items.append(result_item(part))
        elif isinstance(msg, ModelResponse):
            for part in msg.parts:
                if isinstance(part, TextPart) and part.content:
                    items.append(_item("assistant", part.content))
                elif isinstance(part, ThinkingPart) and part.content:
                    items.append(_item("thinking", part.content))
                elif isinstance(part, ToolCallPart):
                    items.append(call_item(part))
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
        label = labels[item.type]
        if label is None:
            continue
        name = f" {item.tool_name}" if item.tool_name else ""
        files = "".join(f" [attachment {a['name']!r} ({a['media_type']})]" for a in item.attachments)
        lines.append(f"[{label}{name}] {truncate(item.text, max_item_chars)}{files}")
    return "\n".join(lines)
