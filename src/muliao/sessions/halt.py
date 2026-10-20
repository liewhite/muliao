"""停止会话（docs/architecture.md「停止会话」）：消费停止、信箱清空，连带停下它发起的异步工作。运行中被停止时在
运行退出之后执行，没在运行时直接执行；停止是会话状态的迁移，一个事务，只写会话自己的数据：

1. 让库里的历史停在不会被续跑的地方：悬空的工具调用（等用户回答的提问除外，用户仍可回答）补上「被停止」的结果，
   末尾没得到回复的请求标上停止。
2. 排队与等重试的运行记为完成，不再执行。
3. 全部 active 占用记为 cleared：之后才到的结局（后台任务、委派）一律不投（sessions.delivery.deliver_in）。
4. 交给这个会话、还没结清的委派结清为「被停止」，结局交回它们的发起方（sessions.delegation）；清空信箱。
5. 被清掉的委派：给做这些工作的 thread 各发一条定向命令 StopThread（逐层停止）。
6. 发布 SessionStopped：后台任务领域结束被清掉的进程。
"""

from __future__ import annotations

import logging
from enum import StrEnum

from pydantic_ai.messages import ModelRequest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agent_runtime.api import is_stopped
from muliao.platform.db import Database
from muliao.platform.messaging import Messaging
from muliao.platform.tables import TaskKind
from muliao.platform.tasks import TaskQueue
from muliao.sessions.codec import dump_message, load_messages
from muliao.sessions.delegation import threads_of_holds_in
from muliao.sessions.events import Ending, SessionStopped, StopThread
from muliao.sessions.history import latest_answer, load_context, marked_stopped, stopped_results
from muliao.sessions.holds import clear_in
from muliao.sessions.lanes import session_lane
from muliao.sessions.mailbox import discard_in, latest_run
from muliao.sessions.settlement import settle_in, unsettled_in
from muliao.sessions.tables import MailState, Message, Session

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


class Halted(StrEnum):
    """停止的结果（命令的结果类型）。"""

    STOPPED = "stopped"
    # 会话已删除（例如 StopThread 停止一个刚被用户删掉的 thread）：契约内的情况，不是错误
    GONE = "gone"


class SessionHalt:
    def __init__(self, db: Database, queue: TaskQueue, messaging: Messaging):
        self._db = db
        self._queue = queue
        self._messaging = messaging

    async def __call__(self, session_id: str) -> Halted:
        async with self._db.transaction() as s:
            session = await s.get(Session, session_id)
            if session is None:
                log.info("stopping a deleted session: nothing to do", extra={"session_id": session_id})
                return Halted.GONE
            await close_history_in(s, session_id)
            dropped = [
                t.id for t in await self._queue.finish_pending_in(s, session_lane(session_id), TaskKind.RUN)
            ]
            cleared = await clear_in(s, session_id)
            settled = await settle_in(
                s,
                self._queue,
                session,
                await unsettled_in(s, session_id, [MailState.QUEUED, MailState.CONSUMED]),
                Ending.STOPPED,
                await latest_answer(s, session_id),
            )
            discarded = await discard_in(s, session_id)
            # 被清掉的委派：做这些工作的 thread 一起停下（逐层，由它们自己的停止继续）
            threads = await threads_of_holds_in(s, cleared)
            for thread_id in threads:
                self._messaging.send_in(s, StopThread(session_id=thread_id))
            self._messaging.publish_in(s, SessionStopped(session_id=session_id, cleared=cleared))
        log.info(
            "session halted",
            extra={
                "session_id": session_id,
                "dropped_runs": dropped,
                "discarded_mail": discarded,
                "cleared_holds": cleared,
                "settled_correlations": settled,
                "stopping_threads": threads,
            },
        )
        return Halted.STOPPED
