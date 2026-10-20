"""信件交给模型的样子：消息是原文加附件引用，事件套上来源（conventions.format_event），迟到的回答写成文字。一次请求里
的信件都并进同一段用户消息，哪些段是事件在构造时就记下（RUN_EVENTS），界面据此单独显示，不从文字反推。"""

from collections.abc import Sequence
from dataclasses import dataclass, replace

from pydantic_ai.messages import ModelRequest, UserContent, UserPromptPart

from muliao.agent_runtime.attachments import copy_to_workspace, references
from muliao.agent_runtime.conventions import RUN_EVENTS, event_indices, format_event
from muliao.agent_runtime.runner import AgentRuntime
from muliao.files.store import FileStore
from muliao.sessions.pending import call_result, late_text
from muliao.sessions.tables import Mail, MailKind


@dataclass(frozen=True)
class Content:
    """一批信件的内容段，以及其中哪些段是事件（下标）。"""

    items: list[UserContent]
    events: list[int]


class ContentBuilder:
    def __init__(self, files: FileStore, runtime: AgentRuntime):
        self._files = files
        self._runtime = runtime

    async def of(self, agent_id: str, mail: Sequence[Mail]) -> Content:
        """回答信件在这里只会是迟到的：按时的回答作为挂起调用的结果交回（sessions.pending）。消息的附件复制到 agent 的
        工作目录（派生副本，原件在文件存储里）。"""
        items: list[UserContent] = []
        events: list[int] = []
        for m in mail:
            if m.kind == MailKind.ANSWER:
                items.append(late_text([call_result(v) for v in m.answers.values()]))
            elif m.kind == MailKind.EVENT:
                if m.source is None:
                    # post_in 校验过事件必有来源
                    raise RuntimeError(f"event mail {m.id} has no source")
                events.append(len(items))
                items.append(format_event(m.source, m.content))
            else:
                files = await self._files.referenced(m.attachments)
                await copy_to_workspace(self._files, files, self._runtime.workspace(agent_id))
                items += [*([m.content] if m.content.strip() else []), *references(files)]
        return Content(items, events)


def append_content(request: ModelRequest, content: Content) -> None:
    """把内容并进这条请求唯一的用户消息（没有就加一条）：一条请求里的信件都在同一段用户消息里，事件下标才有意义。"""
    for i, part in enumerate(request.parts):
        if isinstance(part, UserPromptPart):
            existing = [part.content] if isinstance(part.content, str) else list(part.content)
            request.parts[i] = replace(part, content=[*existing, *content.items])
            mark_events(request, len(existing), content.events)
            return
    request.parts.append(UserPromptPart(content.items))
    mark_events(request, 0, content.events)


def mark_events(request: ModelRequest, offset: int, events: Sequence[int]) -> None:
    """在请求上记下用户消息里哪些段是事件（RUN_EVENTS）。"""
    if events:
        marked = event_indices(request)
        request.metadata = {**(request.metadata or {}), RUN_EVENTS: [*marked, *(offset + i for i in events)]}
