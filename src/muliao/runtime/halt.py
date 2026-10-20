"""停止会话（docs/architecture.md「停止会话」）：消费停止、信箱清空，连带停下它发起的异步工作。运行中被停止时在
运行退出之后执行，没在运行时直接执行；停止是会话状态的迁移，一个事务，只写会话自己的数据：

1. 让库里的历史停在不会被续跑的地方：悬空的工具调用（等用户回答的提问除外，用户仍可回答）补上「被停止」的结果，
   末尾没得到回复的请求标上停止。
2. 排队与等重试的运行记为完成，不再执行。
3. 全部 active 占用记为 cleared：之后才到的结局（后台任务、委派）一律不投（runtime.settlement.deliver_in）。
4. 交给这个会话、还没结清的委派结清为「被停止」；清空信箱。
5. 发布 SessionStopped：后台任务结束被清掉的进程，委派放弃被清掉的工作并逐层停止 thread（各自订阅）。
"""

from __future__ import annotations

import logging

from pydantic_ai.messages import ModelRequest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.conventions import is_stopped
from muliao.db import Database
from muliao.models import MailState, Message, Session, TaskKind
from muliao.platform.messaging import Messaging
from muliao.runtime.codec import dump_message, load_messages
from muliao.runtime.history import latest_answer, load_context, marked_stopped, stopped_results
from muliao.runtime.holds import clear_in
from muliao.runtime.mailbox import discard_in, latest_run
from muliao.runtime.session_events import Ending, SessionStopped
from muliao.runtime.settlement import settle_in, unsettled_in
from muliao.runtime.tasks import TaskQueue, session_lane

log = logging.getLogger(__name__)


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
    def __init__(self, db: Database, queue: TaskQueue, messaging: Messaging):
        self._db = db
        self._queue = queue
        self._messaging = messaging

    async def __call__(self, session_id: str) -> None:
        async with self._db.transaction() as s:
            session = await s.get(Session, session_id)
            if session is None:
                raise RuntimeError(f"session {session_id} vanished while being stopped")
            await close_history_in(s, session_id)
            dropped = [
                t.id for t in await self._queue.finish_pending_in(s, session_lane(session_id), TaskKind.RUN)
            ]
            cleared = await clear_in(s, session_id)
            settled = settle_in(
                s,
                self._messaging,
                session,
                await unsettled_in(s, session_id, [MailState.QUEUED, MailState.CONSUMED]),
                Ending.STOPPED,
                await latest_answer(s, session_id),
            )
            discarded = await discard_in(s, session_id)
            self._messaging.publish_in(s, SessionStopped(session_id=session_id, cleared=cleared))
        log.info(
            "session halted",
            extra={
                "session_id": session_id,
                "dropped_runs": dropped,
                "discarded_mail": discarded,
                "cleared_holds": cleared,
                "settled_correlations": settled,
            },
        )
