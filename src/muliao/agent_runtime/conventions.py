"""我们在 pydantic-ai 消息上加的约定，集中登记在这里：新增约定先在这里登记，解析也只在这里写一份。

pydantic-ai 的消息是存储与回放的唯一形式（数据库里一行一条 ModelMessage），所以「这一轮挂了哪些文件」「这一轮是被
停止的」这类事实都记在消息自己身上，不另存副本。metadata 不发给模型、随消息持久化。

| 约定 | 记在哪里 | 谁写 | 谁读 |
|---|---|---|---|
| `TOOL_ATTACHMENTS`：工具挂给用户的文件 | ToolReturnPart.metadata | share_file、run_temp_agent、ask_agent（agent_runtime.answers） | 会话条目、实时事件、转交给调用方 |
| `TOOL_THREADS`：工具开出或续写的子会话 | ToolReturnPart.metadata | delegate_task、ask_agent | 会话条目、实时事件 |
| 工具交回的图片（`result_images`） | ToolReturnPart.content 列表里的 ImageUrl（文件引用） | attachments.ToolImagesToFiles（如 MCP 截图） | 模型（AttachmentsForModel 展开）、会话条目、实时事件；不转交调用方 |
| `RUN_STOPPED`：运行被用户停止 | 停止时最后保存的那条消息的 metadata | sessions.history | 会话条目（「已停止」）、续跑（不重发标了它的请求） |
| `STOPPED_TOOL_RESULT`：停止时没出结果的调用 | 合成的 ToolReturnPart 内容 | sessions.history | 模型（之后的运行）；会话条目（made_up_result：没有结束时间） |
| `INTERRUPTED_TOOL_RESULT`：进程退出时没出结果的调用 | 合成的 ToolReturnPart 内容（`outcome='interrupted'`） | sessions.history | 模型（续跑的那次请求）；会话条目（同上） |
| `questions.NOT_ANSWERED`：挂起提问没被回答 | 合成的 ask_user 结果（AskResult，属于提问领域） | sessions.questions 定义、sessions.deferred 填入 | 模型、会话条目 |
| 「调用是否已有结果」 | ToolReturnPart 或 RetryPromptPart | — | unanswered_calls：挂起的提问、压缩切点、停止时补结果 |
| `RUN_EVENTS`：这一步交给模型的事件信件 | 事件各占用户消息内容的一段（format_event），metadata 记下是第几段 | sessions.content | 模型、会话条目 |
| `RUN_CONTEXT`：这次运行的运行上下文（当前时间等） | 单独一段用户消息，metadata 记下它是第几个 part | sessions.steps | 模型；会话条目不展示它 |

metadata 的键都带 `muliao_` 前缀：metadata 是 pydantic-ai 开放给所有工具（含 MCP 包装）的字段，通用的键名可能撞上形状
不同的数据。
"""

import re
from collections.abc import Sequence
from typing import TypedDict

from pydantic import TypeAdapter
from pydantic_ai.messages import (
    DocumentUrl,
    ImageUrl,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from muliao.files.api import FileMeta, parse_file_url

# 用户停止的一轮：它保存下来的最后一条消息的 metadata 带这个标记，展示为「已停止」
RUN_STOPPED = "muliao_stopped"
# 停止时还没出结果的工具调用的结果：工具的副作用可能已经发生，告诉模型它没有完成
STOPPED_TOOL_RESULT = "Stopped by the user before this tool call finished; it may have partly run."
# 一步在执行工具时被打断（进程退出，或这一步失败）：续跑不重新执行这些调用——副作用可能已经发生，而我们无从
# 知道——补上这个结果交给模型判断。pydantic-ai 的 outcome='interrupted' 说的就是这件事：没有产生结果，不是工具报错
INTERRUPTED_TOOL_RESULT = (
    "Interrupted: this run was interrupted while the call was running (the service restarted, or the "
    "step failed), and the call was NOT re-run automatically. It may or may not have taken effect; "
    "check its effects before deciding whether to run it again."
)


# 交给模型的事件信件（sessions.mailbox）：每条事件是用户消息内容里独立的一段，格式见 format_event；
# 那条 ModelRequest 的 metadata[RUN_EVENTS] 是这些段在内容里的下标
RUN_EVENTS = "muliao_events"
_EVENT = re.compile(r'\A<event source="(?P<source>[^"]*)">\n(?P<content>.*)\n</event>\Z', re.DOTALL)


def format_event(source: str, content: str) -> str:
    """交给模型的一条事件。source 是发送方（如 job:12），不含引号（sessions.mailbox 校验）。"""
    return f'<event source="{source}">\n{content}\n</event>'


def parse_event(text: str) -> tuple[str, str]:
    """format_event 的逆运算；对不上就是数据与代码不一致。"""
    match = _EVENT.match(text)
    if match is None:
        raise RuntimeError(f"stored event does not follow the event format: {text[:200]!r}")
    return match["source"], match["content"]


# 一次运行开头交给模型的运行上下文（当前时间等，见 agent_runtime.instructions 的模块说明）：它单独占一段用户
# 消息，那条 ModelRequest 的 metadata[RUN_CONTEXT] 是它在 parts 里的下标。只挂在这次运行新增的第一条请求上，
# 之后随消息入库、原样重放——旧消息字节不变，缓存才命中
RUN_CONTEXT = "muliao_run_context"


def attach_run_context(request: ModelRequest, text: str) -> None:
    """把运行上下文作为独立的一段用户消息放进这条请求，并记下它是第几个 part。

    放在用户内容之前、工具结果之后：模型先看到「现在几点」再看这次要处理的内容；而一条请求里工具结果在前、用户内容
    在后是这里已有的形状（sessions.content.append_content），不为这一段改变它。"""
    if RUN_CONTEXT in (request.metadata or {}):
        raise RuntimeError(f"request already carries a run context: {request.metadata!r}")
    parts = list(request.parts)
    index = next((i for i, p in enumerate(parts) if isinstance(p, UserPromptPart)), len(parts))
    parts.insert(index, UserPromptPart(text))
    request.parts = parts
    request.metadata = {**(request.metadata or {}), RUN_CONTEXT: index}


def run_context_index(message: ModelMessage) -> int | None:
    """这条消息里哪个 part 是运行上下文（RUN_CONTEXT）；没有带就是 None。"""
    return (message.metadata or {}).get(RUN_CONTEXT)


def is_stopped(message: ModelMessage) -> bool:
    """这条消息标着「已停止」（RUN_STOPPED）。"""
    return bool((message.metadata or {}).get(RUN_STOPPED))


def answer_text(response: ModelResponse) -> str:
    """一条模型回复给用户的文字：它的文本部分按顺序连起来（会话最近的回答、运行的回答都按这一条取）。"""
    return "".join(p.content for p in response.parts if isinstance(p, TextPart))


def event_indices(message: ModelMessage) -> list[int]:
    """这条消息里哪些内容段是会话事件（RUN_EVENTS）；没有事件为空。"""
    return list((message.metadata or {}).get(RUN_EVENTS, []))


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


def made_up_result(part: ToolReturnPart) -> bool:
    """这个结果是事后补上的（停止、中断时没出结果的调用），不是工具交回的：它产生的时间不是调用结束的时间。"""
    return part.outcome == "interrupted" or part.content == STOPPED_TOOL_RESULT


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


def attachment_of_ref(ref: ImageUrl | DocumentUrl) -> Attachment:
    """消息里一个文件引用（存储形态）的展示形式：图片内联，其余是下载链接。"""
    return Attachment(
        url=ref.url,
        name=parse_file_url(ref.url)[1],
        media_type=ref.media_type,
        inline=isinstance(ref, ImageUrl),
    )


def result_image_refs(part: object) -> list[ImageUrl]:
    """工具交回、已存进文件存储的图片引用（attachments.ToolImagesToFiles 写下：有图片的结果总是内容列表）。"""
    if not isinstance(part, ToolReturnPart) or not isinstance(part.content, list):
        return []
    return [c for c in part.content if isinstance(c, ImageUrl)]


def result_images(part: ToolReturnPart | RetryPromptPart) -> list[Attachment]:
    """工具交回的图片的展示形式。它们是给模型看的结果，会话里显示在这次调用的过程里（Item.images）；不是挂给用户的
    文件（TOOL_ATTACHMENTS），所以不进 shared_attachments、不转交调用方。"""
    return [attachment_of_ref(r) for r in result_image_refs(part)]


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


def calls_in(message: ModelMessage) -> list[ToolCallPart]:
    """这条消息发出的工具调用（只有模型回复会有）。"""
    return (
        [p for p in message.parts if isinstance(p, ToolCallPart)]
        if isinstance(message, ModelResponse)
        else []
    )


def results_in(message: ModelMessage) -> set[str]:
    """这条消息给出结果的调用 id：ToolReturnPart，或参数被拒绝、交还模型重试的 RetryPromptPart。"""
    if not isinstance(message, ModelRequest):
        return set()
    return {p.tool_call_id for p in message.parts if isinstance(p, ToolReturnPart | RetryPromptPart)}


def unanswered_calls(messages: Sequence[ModelMessage]) -> list[ToolCallPart]:
    """还没有结果的工具调用，按出现顺序。「调用是否已有结果」只在这里定义（calls_in / results_in；挂起的调用、
    压缩切点、停止时补结果都用它）。"""
    done = set().union(*(results_in(m) for m in messages))
    return [c for m in messages for c in calls_in(m) if c.tool_call_id not in done]
