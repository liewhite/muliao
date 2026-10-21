"""会话历史：发给模型的上下文（最新摘要 + 压缩切点之后的原始消息）。原始消息不删除，压缩只决定从哪里开始发。"""

from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
from typing import Literal

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agent_runtime.api import (
    INTERRUPTED_TOOL_RESULT,
    RUN_STOPPED,
    STOPPED_TOOL_RESULT,
    answer_text,
    unanswered_calls,
)
from muliao.sessions.codec import load_messages
from muliao.sessions.questions import ASK_USER
from muliao.sessions.tables import Compaction, Message


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


async def since_last_response_among(
    s: AsyncSession, session_ids: Collection[str]
) -> dict[str, list[ModelMessage]]:
    """since_last_response 的批量版本：先一次查询取各会话的最后一条消息，多数会话停在一条模型回复上，就是它；
    停在请求上的（运行中途、出错）再逐个往回读。"""
    last_ids = (
        select(func.max(Message.id))
        .where(Message.session_id.in_(list(session_ids)))
        .group_by(Message.session_id)
    )
    rows = (
        await s.execute(select(Message.session_id, Message.payload).where(Message.id.in_(last_ids)))
    ).all()
    tails: dict[str, list[ModelMessage]] = {x: [] for x in session_ids}
    for session_id, payload in rows:
        (message,) = load_messages([payload])
        tails[session_id] = (
            [message] if isinstance(message, ModelResponse) else await since_last_response(s, session_id)
        )
    return tails


async def since_last_response(s: AsyncSession, session_id: str) -> list[ModelMessage]:
    """从最后一条模型回复起的消息（按顺序；还没有回复时是全部）。之前每条回复的工具调用在下一条回复之前都已有
    结果（提供方的要求），所以还没有结果的调用（挂起的提问）只可能在这一段里：查它们不必加载整段上下文。"""
    tail: list[ModelMessage] = []
    before: int | None = None
    while True:
        # 分批往回读：通常一两条就到最后一条回复，不把整段历史读进内存
        query = select(Message.id, Message.payload).where(Message.session_id == session_id)
        if before is not None:
            query = query.where(Message.id < before)
        rows = (await s.execute(query.order_by(Message.id.desc()).limit(_TAIL_BATCH))).all()
        for message_id, payload in rows:
            (message,) = load_messages([payload])
            tail.append(message)
            if isinstance(message, ModelResponse):
                return tail[::-1]
            before = message_id
        if len(rows) < _TAIL_BATCH:
            return tail[::-1]


_TAIL_BATCH = 4


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
    tail = await since_last_response(s, session_id)
    return answer_text(tail[0]) if tail and isinstance(tail[0], ModelResponse) else ""


def _results_for(
    calls: Sequence[ToolCallPart], content: str, outcome: Literal["success", "interrupted"]
) -> list[ModelMessage]:
    """给这些没出结果的调用补上同一个结果，合成一条请求；没有调用则为空。"""
    if not calls:
        return []
    return [
        ModelRequest(
            parts=[
                ToolReturnPart(c.tool_name, content, tool_call_id=c.tool_call_id, outcome=outcome)
                for c in calls
            ]
        )
    ]


def stopped_results(history: Sequence[ModelMessage]) -> list[ModelMessage]:
    """没出结果的工具调用补上「被用户停止、可能已部分执行」的结果；等用户回答的提问除外（它在等人回答，不是没做完
    的调用，用户仍可回答）。"""
    return _results_for(
        [c for c in unanswered_calls(history) if c.tool_name != ASK_USER], STOPPED_TOOL_RESULT, "success"
    )


def interrupted_results(calls: Sequence[ToolCallPart]) -> list[ModelMessage]:
    """一步在执行工具时被打断，这些调用没有结果：补上「被中断、没有重新执行」的结果。它们不重新执行——副作用
    （shell、通知、委派、改记忆）可能已经发生，而我们无从知道——由模型看着结果决定要不要再做一次。哪些调用走这条
    路由 Consumer 决定（sessions.consumer.interrupted_calls），设计见 docs/architecture.md「从哪里开始」。"""
    return _results_for(calls, INTERRUPTED_TOOL_RESULT, "interrupted")


def marked_stopped(message: ModelMessage) -> ModelMessage:
    """标上「已停止」（RUN_STOPPED）：界面据此显示，续跑据此不重发这条请求。"""
    return replace(message, metadata={**(message.metadata or {}), RUN_STOPPED: True})
