"""会话历史：发给模型的上下文（最新摘要 + 压缩切点之后的原始消息）。原始消息不删除，压缩只决定从哪里开始发。"""

from collections.abc import Sequence
from dataclasses import dataclass, replace

from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolReturnPart, UserPromptPart
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.models import Compaction, Message
from muliao.sessions.codec import load_messages
from muliao.sessions.conventions import RUN_STOPPED, STOPPED_TOOL_RESULT, answer_text, unanswered_calls
from muliao.sessions.questions import ASK_USER


@dataclass(frozen=True)
class SessionContext:
    """发给模型的 session 上下文。"""

    summary: str | None
    messages: list[ModelMessage]
    # 最近一次压缩写入之前就有的模型回复条数（在 messages 里）：它们报告的 token 数是压缩前的上下文大小，
    # 判断要不要再压缩只看之后的回复（sessions.compaction.Compactor.verdict）；没压缩过为 None
    stale_responses: int | None


async def latest_compaction(s: AsyncSession, session_id: str) -> Compaction | None:
    return await s.scalar(
        select(Compaction).where(Compaction.session_id == session_id).order_by(Compaction.id.desc()).limit(1)
    )


async def rows_after(s: AsyncSession, session_id: str, after_id: int) -> list[Message]:
    query = (
        select(Message).where(Message.session_id == session_id, Message.id > after_id).order_by(Message.id)
    )
    return list((await s.scalars(query)).all())


# 压缩的切点可能落在一条模型回复之前（sessions.compaction.steps_of），而提供方要求对话从用户消息开始：这时在留下的
# 部分前面补这一条。它由切点推导，不存库
CONTINUATION = (
    "The earlier part of this conversation is summarized in your instructions. Continue from there."
)


async def load_context(s: AsyncSession, session_id: str) -> SessionContext:
    compaction = await latest_compaction(s, session_id)
    rows = await rows_after(s, session_id, compaction.through_message_id if compaction else 0)
    messages = load_messages(r.payload for r in rows)
    stale = (
        None
        if compaction is None
        else sum(
            1
            for r, m in zip(rows, messages, strict=True)
            if r.id <= compaction.at_message_id and isinstance(m, ModelResponse)
        )
    )
    if messages and isinstance(messages[0], ModelResponse):
        messages.insert(0, ModelRequest(parts=[UserPromptPart(CONTINUATION)]))
    return SessionContext(
        summary=compaction.summary if compaction else None, messages=messages, stale_responses=stale
    )


async def latest_answer(s: AsyncSession, session_id: str) -> str:
    """会话最近的回答：最后一条模型回复的文字；还没有回复时为空（运行停在第一次回复之前）。"""
    payloads = await s.scalars(
        select(Message.payload).where(Message.session_id == session_id).order_by(Message.id.desc())
    )
    for payload in payloads:
        (message,) = load_messages([payload])
        if isinstance(message, ModelResponse):
            return answer_text(message)
    return ""


def stopped_results(history: Sequence[ModelMessage]) -> list[ModelMessage]:
    """没出结果的工具调用（等用户回答的提问除外）补上「被用户停止、可能已部分执行」的结果；没有则为空。"""
    dangling = [c for c in unanswered_calls(history) if c.tool_name != ASK_USER]
    if not dangling:
        return []
    return [
        ModelRequest(
            parts=[
                ToolReturnPart(c.tool_name, STOPPED_TOOL_RESULT, tool_call_id=c.tool_call_id)
                for c in dangling
            ]
        )
    ]


def marked_stopped(message: ModelMessage) -> ModelMessage:
    """标上「已停止」（RUN_STOPPED）：界面据此显示，续跑据此不重发这条请求。"""
    return replace(message, metadata={**(message.metadata or {}), RUN_STOPPED: True})
