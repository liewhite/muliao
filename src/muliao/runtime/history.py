"""会话历史：发给模型的上下文（最新摘要 + 压缩切点之后的原始消息）。原始消息不删除，压缩只决定从哪里开始发。"""

from dataclasses import dataclass

from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, UserPromptPart
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.conventions import answer_text
from muliao.models import Compaction, Message
from muliao.runtime.codec import load_messages


@dataclass(frozen=True)
class SessionContext:
    """发给模型的 session 上下文。"""

    summary: str | None
    messages: list[ModelMessage]


async def latest_compaction(s: AsyncSession, session_id: str) -> Compaction | None:
    return await s.scalar(
        select(Compaction).where(Compaction.session_id == session_id).order_by(Compaction.id.desc()).limit(1)
    )


async def rows_after(s: AsyncSession, session_id: str, after_id: int) -> list[Message]:
    query = (
        select(Message).where(Message.session_id == session_id, Message.id > after_id).order_by(Message.id)
    )
    return list((await s.scalars(query)).all())


# 压缩的切点可能落在一条模型回复之前（runtime.compaction.steps_of），而提供方要求对话从用户消息开始：这时在留下的
# 部分前面补这一条。它由切点推导，不存库
CONTINUATION = (
    "The earlier part of this conversation is summarized in your instructions. Continue from there."
)


async def load_context(s: AsyncSession, session_id: str) -> SessionContext:
    compaction = await latest_compaction(s, session_id)
    rows = await rows_after(s, session_id, compaction.through_message_id if compaction else 0)
    messages = load_messages(r.payload for r in rows)
    if messages and isinstance(messages[0], ModelResponse):
        messages.insert(0, ModelRequest(parts=[UserPromptPart(CONTINUATION)]))
    return SessionContext(summary=compaction.summary if compaction else None, messages=messages)


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
