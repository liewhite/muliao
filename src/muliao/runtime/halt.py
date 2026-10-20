"""停止会话（docs/architecture.md「停止会话」）：消费停止、信箱清空，连带停下它发起的异步工作。运行中被停止与没在
运行时停止共用这一处，停止是会话状态的迁移，不依赖有没有运行在跑：

1. 停下会话发起的异步工作（后台任务、委派出去的 thread，runtime.work）：之后结束的工作不再给它投信。
2. 让库里的历史停在不会被续跑的地方：悬空的工具调用（等用户回答的提问除外，用户仍可回答）补上「被停止」的结果，
   末尾没得到回复的请求标上停止。
3. 排队与等重试的运行记为完成，不再执行。
4. 交给这个会话、还没交回的委派交回「被停止」；清空信箱。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace

from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.conventions import RUN_STOPPED, STOPPED_TOOL_RESULT, is_stopped, unanswered_calls
from muliao.db import Database
from muliao.models import Message, Session, TaskKind
from muliao.runtime.codec import dump_message, load_messages
from muliao.runtime.delegations import Ending, Outcome, in_session_in, settle_all_in
from muliao.runtime.history import latest_answer, load_context
from muliao.runtime.mailbox import discard_in, latest_run
from muliao.runtime.questions import ASK_USER
from muliao.runtime.tasks import TaskQueue, session_lane
from muliao.runtime.work import SessionWork, halt_all

log = logging.getLogger(__name__)


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


async def close_history_in(s: AsyncSession, session_id: str) -> None:
    """停止时让会话历史停在不会被续跑的地方（唯一事实源是库里的历史）。运行中被停止时运行已经保存了这些，这里没事可做。"""
    history = (await load_context(s, session_id)).messages
    if closing := stopped_results(history):
        run = await latest_run(s, session_id)
        if run is None:
            raise RuntimeError(f"session {session_id} has tool calls but no run")
        s.add(
            Message(session_id=session_id, task_id=run.id, payload=dump_message(marked_stopped(closing[0])))
        )
        return
    if history and isinstance(history[-1], ModelRequest) and not is_stopped(history[-1]):
        row = await s.scalar(
            select(Message).where(Message.session_id == session_id).order_by(Message.id.desc()).limit(1)
        )
        if row is None:
            raise RuntimeError(f"session {session_id} has history but no message rows")
        (message,) = load_messages([row.payload])
        row.payload = dump_message(marked_stopped(message))


class SessionHalt:
    def __init__(self, db: Database, queue: TaskQueue, works: Sequence[SessionWork]):
        self._db = db
        self._queue = queue
        self._works = works

    async def __call__(self, session_id: str) -> None:
        await halt_all(self._works, session_id)
        async with self._db.transaction() as s:
            session = await s.get(Session, session_id)
            if session is None:
                raise RuntimeError(f"session {session_id} vanished while being stopped")
            await close_history_in(s, session_id)
            dropped = [
                t.id for t in await self._queue.finish_pending_in(s, session_lane(session_id), TaskKind.RUN)
            ]
            delegations = await in_session_in(s, session_id)
            await settle_all_in(
                s,
                self._queue,
                delegations,
                session,
                Outcome(Ending.STOPPED, await latest_answer(s, session_id)),
            )
            discarded = await discard_in(s, session_id)
        log.info(
            "session halted",
            extra={
                "session_id": session_id,
                "dropped_runs": dropped,
                "discarded_mail": discarded,
                "settled_delegations": [d.id for d in delegations],
            },
        )
