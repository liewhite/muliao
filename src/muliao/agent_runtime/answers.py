"""一个 agent 替另一个 agent 做完的结果（ask_agent 的结局），以及把它作为工具结果交回调用方的唯一方式。

被调用方用工具挂给用户的文件（conventions.TOOL_ATTACHMENTS）只记在被调用方自己的消息里，私有 thread 用户也不会主动
去翻。所以交回时把这些文件挂到调用方这次工具调用的结果上，文件就出现在用户正在看的会话里。
"""

from dataclasses import dataclass

from pydantic_ai import ToolReturn

from muliao.agent_runtime.conventions import TOOL_ATTACHMENTS, TOOL_THREADS, Attachment


@dataclass(frozen=True)
class Answer:
    text: str
    attachments: list[Attachment]

    def to_tool_return(self, thread_id: str) -> ToolReturn:
        """给调用方模型的是回答文字，并告诉它哪些文件已经显示给用户、不用再转发。thread_id 是回答所在的子会话。"""
        note = ""
        if self.attachments:
            names = ", ".join(repr(a["name"]) for a in self.attachments)
            note = f"\n\n[Shared {names} with the user; they are shown with your reply automatically.]"
        metadata: dict[str, object] = {TOOL_ATTACHMENTS: self.attachments, TOOL_THREADS: [thread_id]}
        return ToolReturn(return_value=self.text + note, metadata=metadata)
