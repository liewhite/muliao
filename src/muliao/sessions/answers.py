"""一个 agent 替另一个 agent 做完的结果（ask_agent 的结局，sessions.delegation 交回）与它在回答信件里的信封。ask_agent
是一种挂起的调用，挂起、唤醒与交回的规则见 sessions.pending（同 questions 之于 ask_user、approvals 之于等批准）。

被调用方用工具挂给用户的文件（agent_runtime 的 TOOL_ATTACHMENTS）只记在被调用方自己的消息里，thread 用户不一定会
去翻。所以交回时把这些文件挂到调用方这次工具调用的结果上，文件就出现在用户正在看的会话里。
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Literal

from pydantic import BaseModel
from pydantic_ai import ToolReturn

from muliao.agent_runtime.api import TOOL_ATTACHMENTS, Attachment

# ask_agent 答复之前来了新消息：这次调用不再等，模型知道回答之后会作为事件送达
STILL_WORKING = (
    "A new message arrived before the agent you asked answered. Its answer will reach you as an event "
    "in this conversation when it is ready."
)


class AgentAnswer(BaseModel):
    """被问的 agent 交回的结局（sessions.delegation）：给发问方模型的文字，它挂给用户的文件。做它的 thread 不在这里：
    调用开出了哪个 thread 记在 sessions.thread_calls。更早投出的回答信件里的 thread 键忽略。"""

    text: str
    attachments: list[Attachment]


class AgentAnswered(BaseModel):
    """回答信件里一次 ask_agent 调用的结果：被问的 agent 的结局。"""

    tool: Literal["ask_agent"]
    result: AgentAnswer

    @classmethod
    def of(cls, result: AgentAnswer) -> AgentAnswered:
        return cls(tool="ask_agent", result=result)

    def tool_return(self) -> object:
        """给调用方模型的是回答文字，并告诉它哪些文件已经显示给用户、不用再转发。"""
        attachments = list(self.result.attachments)
        note = ""
        if attachments:
            names = ", ".join(repr(a["name"]) for a in attachments)
            note = f"\n\n[Shared {names} with the user; they are shown with your reply automatically.]"
        return ToolReturn(return_value=self.result.text + note, metadata={TOOL_ATTACHMENTS: attachments})

    @staticmethod
    def skipped(given: Collection[object]) -> object:
        """没等到回答就继续时的结果（given 是这次交回的别的回答，不看）：回答之后作为事件送达。"""
        return STILL_WORKING

    @staticmethod
    def late_lines(answers: Sequence[AgentAnswered]) -> list[str]:
        return [f"An agent you asked earlier answered:\n{a.result.text}" for a in answers]
