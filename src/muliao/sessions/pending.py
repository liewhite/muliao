"""挂起的工具调用：模型调用了一个要等别人回信的工具（ask_user 等用户、ask_agent 等另一个 agent），工具抛出
pydantic-ai 的 CallDeferred，这次运行停在这里正常结束，不在 worker 里等。回信是一封回答信件（MailKind.ANSWER），
作为那次调用的结果（DeferredToolResults）交回，模型看到的就是「我问了，对方这样答」。设计见 docs/architecture.md
「挂起的调用」。

- 哪些调用还挂着由历史推导（pending_calls），不另存状态。
- 什么时候继续（waiting，唯一规则）：用户发了新消息（跳过还没回答的），或挂起的调用都有了回答；事件留在信箱里
  等它们一起交给模型。唤醒（mailbox.wake_in）与消费者开头共用这一条。
- 跳过：没回答的 ask_user 记为没回答（questions），还没回答的 ask_agent 记为「回答之后作为事件送达」——那时
  它已不再挂着，委派的结局按事件投回（delegations.manager）。
"""

from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from typing import Final

from pydantic import BaseModel, TypeAdapter
from pydantic_ai import ToolReturn
from pydantic_ai.messages import ModelMessage, ToolCallPart
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.sessions.answers import Answer
from muliao.sessions.conventions import Attachment, unanswered_calls
from muliao.sessions.history import load_context
from muliao.sessions.holds import called_in
from muliao.sessions.questions import (
    ASK_USER,
    NOT_ANSWERED,
    SKIPPED,
    AskResult,
    Question,
    late_answer_lines,
    questions_of,
)
from muliao.sessions.tables import Mail, MailKind

ASK_AGENT: Final = "ask_agent"
DEFERRED_TOOLS: Final = frozenset({ASK_USER, ASK_AGENT})


class AgentAnswer(BaseModel):
    """被问的 agent 交回的结局（delegations.manager）：给发问方模型的文字，它挂给用户的文件，所在的 thread。"""

    text: str
    attachments: list[Attachment]
    thread: str

    def to_tool_return(self) -> ToolReturn:
        return Answer(self.text, list(self.attachments)).to_tool_return(thread_id=self.thread)


# 回答信件里一次调用的结果：ask_user 是用户的回答，ask_agent 是对方的结局（两者字段不重叠，按形状区分）
CallResult = AskResult | AgentAnswer
_CALL_RESULT: TypeAdapter[CallResult] = TypeAdapter(CallResult)


def call_result(value: object) -> CallResult:
    """读回信件里存的结果；不合法是数据与代码不一致。"""
    return _CALL_RESULT.validate_python(value)


# ask_agent 答复之前来了新消息：这次调用不再等，模型知道回答之后会作为事件送达
STILL_WORKING = (
    "A new message arrived before the agent you asked answered. Its answer will reach you as an event "
    "in this conversation when it is ready."
)

# 这个会话里已经交出去的 ask_agent 调用 id（带 call_id 的占用是事实来源，sessions.holds.called_in）
Delegated = AbstractSet[str]


def pending_calls(history: Sequence[ModelMessage], delegated: Delegated) -> dict[str, ToolCallPart]:
    """还在等回信的调用，按出现顺序：没有结果的 ask_user 调用（参数被拒绝、交还模型重试的有结果，不算），以及已经
    交出去、还没有结果的 ask_agent 调用。没有占用的 ask_agent 不是在等回信：工具还没执行完（回复保存后、投递
    提交前失败），它是待执行的调用，续跑时再执行（Threads.dispatch 按调用 id 幂等）。"""
    return {
        c.tool_call_id: c
        for c in unanswered_calls(history)
        if c.tool_name == ASK_USER or (c.tool_name == ASK_AGENT and c.tool_call_id in delegated)
    }


def pending_questions(history: Sequence[ModelMessage]) -> dict[str, list[Question]]:
    """挂起的 ask_user 调用里的问题。"""
    return {c.tool_call_id: questions_of(c) for c in unanswered_calls(history) if c.tool_name == ASK_USER}


async def pending_in(s: AsyncSession, session_id: str) -> dict[str, ToolCallPart]:
    """会话现在挂着的调用，从库里的历史推导（压缩的切点不会落在挂起的调用之前）。"""
    return pending_calls((await load_context(s, session_id)).messages, await called_in(s, session_id))


def answered_in(queued: Sequence[Mail], pending: Mapping[str, ToolCallPart]) -> dict[str, CallResult]:
    """信箱里对这些挂起调用的回答。回答信件里不针对它们的（迟到的，提问已被别的运行关闭）不在这里，见 is_on_time。"""
    return {
        call_id: call_result(value)
        for m in queued
        if is_on_time(m, pending)
        for call_id, value in m.answers.items()
    }


def is_on_time(mail: Mail, pending: Mapping[str, ToolCallPart]) -> bool:
    """这封回答信件针对的调用都还挂着：作为那些调用的结果交回。否则它是迟到的，作为文字交给模型（late_text）。"""
    return mail.kind == MailKind.ANSWER and set(mail.answers) <= set(pending)


def waiting(history: Sequence[ModelMessage], queued: Sequence[Mail], delegated: Delegated) -> bool:
    """停在挂起的调用上、还不能继续：有挂起的调用，没有新消息，回答也没覆盖全部挂起的调用。"""
    pending = pending_calls(history, delegated)
    if not pending or any(m.kind == MailKind.MESSAGE for m in queued):
        return False
    return not set(pending) <= set(answered_in(queued, pending))


def deferred_results(
    pending: Mapping[str, ToolCallPart], answers: Mapping[str, CallResult], delegated: Delegated
) -> dict[str, object]:
    """交给 pydantic-ai 的挂起调用结果。有回答的用回答；其余（来了新消息，或这次只回答了一部分）：ask_user 记为
    没回答（这次回答了别的提问时是跳过了它们），ask_agent 记为回答之后作为事件送达。"""
    any_answered = any(isinstance(r, AskResult) for r in answers.values())
    results: dict[str, object] = {}
    for call_id, call in pending.items():
        result = answers.get(call_id)
        if isinstance(result, AgentAnswer):
            results[call_id] = result.to_tool_return()
        elif isinstance(result, AskResult):
            results[call_id] = result.model_dump()
        elif call.tool_name == ASK_USER:
            results[call_id] = (SKIPPED if any_answered else NOT_ANSWERED).model_dump()
        else:
            results[call_id] = STILL_WORKING
    return results


def late_text(results: Sequence[CallResult]) -> str:
    """回答到达时那次调用已经结束（模型已经带着「没回答」或「之后送达」继续了）：作为一段用户消息交给模型，回答不丢。"""
    lines: list[str] = []
    asked = [r for r in results if isinstance(r, AskResult)]
    if asked:
        lines += ["The user answered questions that had already been closed:", *late_answer_lines(asked)]
    lines += [
        f"An agent you asked earlier answered:\n{r.text}" for r in results if isinstance(r, AgentAnswer)
    ]
    return "\n".join(lines)
