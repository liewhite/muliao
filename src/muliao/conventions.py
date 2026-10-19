"""我们在 pydantic-ai 消息上加的约定，集中登记在这里：新增约定先在这里登记，解析也只在这里写一份。

pydantic-ai 的消息是存储与回放的唯一形式（数据库里一行一条 ModelMessage），所以「这一轮挂了哪些文件」「这一轮是被
停止的」这类事实都记在消息自己身上，不另存副本。metadata 不发给模型、随消息持久化。

| 约定 | 记在哪里 | 谁写 | 谁读 |
|---|---|---|---|
| `TOOL_ATTACHMENTS`：工具挂给用户的文件 | ToolReturnPart.metadata | share_file、run_temp_agent、ask_agent（runtime.answers） | 会话条目、实时事件、转交给调用方 |
| `TOOL_THREADS`：工具开出或续写的子会话 | ToolReturnPart.metadata | delegate_task、ask_agent | 会话条目、实时事件 |
| `TURN_STOPPED`：这一轮被用户停止 | 停止的一轮最后一条消息的 metadata | runtime.stopping | 会话条目（「已停止」） |
| `STOPPED_TOOL_RESULT`：停止时没出结果的调用 | 合成的 ToolReturnPart 内容 | runtime.stopping | 模型（下一轮） |
| `questions.NOT_ANSWERED`：挂起提问没被回答 | 合成的 ask_user 结果（AskResult，属于提问领域） | runtime.turn | 模型、会话条目 |
| 「调用是否已有结果」 | ToolReturnPart 或 RetryPromptPart | — | unanswered_calls：挂起的提问、压缩切点、停止时补结果 |

metadata 的键都带 `muliao_` 前缀：metadata 是 pydantic-ai 开放给所有工具（含 MCP 包装）的字段，通用的键名可能撞上形状
不同的数据。
"""

from collections.abc import Sequence
from typing import TypedDict

from pydantic import TypeAdapter
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
)

from muliao.files import FileMeta, parse_file_url

# 用户停止的一轮：它保存下来的最后一条消息的 metadata 带这个标记，展示为「已停止」
TURN_STOPPED = "muliao_stopped"
# 停止时还没出结果的工具调用的结果：工具的副作用可能已经发生，告诉模型它没有完成
STOPPED_TOOL_RESULT = "Stopped by the user before this tool call finished; it may have partly run."


class Attachment(TypedDict):
    # files.file_url：界面直接用它显示与下载
    url: str
    name: str
    media_type: str
    # 能内联显示的图片（files.INLINE_IMAGE_TYPES）；其余是下载链接
    inline: bool


# ToolReturn(metadata={TOOL_ATTACHMENTS: [Attachment, ...]})：文件一定会显示给用户，不依赖模型在回答里转写引用
TOOL_ATTACHMENTS = "muliao_attachments"
_ATTACHMENTS = TypeAdapter(list[Attachment])
# ToolReturn(metadata={TOOL_THREADS: [session_id, ...]})：界面把 thread 显示在开出或续写它的那次调用处
TOOL_THREADS = "muliao_threads"
_THREADS = TypeAdapter(list[str])


def attachment_of(meta: FileMeta) -> Attachment:
    return Attachment(url=meta.url, name=meta.name, media_type=meta.media_type, inline=meta.is_inline_image)


def tool_attachments(part: ToolReturnPart | RetryPromptPart) -> list[Attachment]:
    """工具结果挂上的文件（历史条目与实时事件共用这一处解析）。其他工具或 MCP 的 metadata 不带这个键时为空。"""
    if not isinstance(part, ToolReturnPart) or not isinstance(part.metadata, dict):
        return []
    if TOOL_ATTACHMENTS not in part.metadata:
        return []
    attachments = _ATTACHMENTS.validate_python(part.metadata[TOOL_ATTACHMENTS])
    # 前端把 url 直接放进 src/href：只能是本站文件存储的引用
    for a in attachments:
        parse_file_url(a["url"])
    return attachments


def tool_threads(part: ToolReturnPart | RetryPromptPart) -> list[str]:
    """工具结果关联的子会话 id（历史条目与实时事件共用这一处解析）。"""
    if not isinstance(part, ToolReturnPart) or not isinstance(part.metadata, dict):
        return []
    return _THREADS.validate_python(part.metadata.get(TOOL_THREADS, []))


def shared_attachments(messages: Sequence[ModelMessage]) -> list[Attachment]:
    """这些消息里所有工具结果挂上的文件，按出现顺序。函数工具的结果只出现在 ModelRequest 里。"""
    return [
        a
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, ToolReturnPart)
        for a in tool_attachments(p)
    ]


def unanswered_calls(messages: Sequence[ModelMessage]) -> list[ToolCallPart]:
    """还没有结果的工具调用，按出现顺序。结果是 ToolReturnPart，或参数被拒绝、交还模型重试的 RetryPromptPart。
    「调用是否已有结果」只在这里定义（挂起的提问、压缩切点都用它）。"""
    done = {
        p.tool_call_id
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, ToolReturnPart | RetryPromptPart)
    }
    return [
        p
        for m in messages
        if isinstance(m, ModelResponse)
        for p in m.parts
        if isinstance(p, ToolCallPart) and p.tool_call_id not in done
    ]
