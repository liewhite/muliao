"""停止会话（docs/architecture.md「停止会话」）：消费停止、信箱清空，连带停下它发起的异步工作。运行中被停止时在
运行退出之后执行，没在运行时直接执行；停止是会话状态的迁移，一个事务，只写会话自己的数据：

1. 让库里的历史停在不会被续跑的地方：悬空的工具调用（等用户回答的提问除外，用户仍可回答）补上「被停止」的结果，
   末尾没得到回复的请求标上停止。
2. 排队与等重试的运行记为完成，不再执行。
3. 全部 active 占用记为 cleared：之后才到的结局（后台任务、委派）一律不投（sessions.work.deliver_in）。
4. 交给这个会话、还没结清的委派结清为「被停止」，结局交回它们的发起方（sessions.delegation）；清空信箱。
5. 被清掉的委派：给做这些工作的 thread 各发一条定向命令 StopThread（逐层停止）。
6. 发布 SessionStopped：后台任务领域结束被清掉的进程。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from enum import StrEnum

from pydantic_ai.messages import ModelRequest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.platform.db import Database
from muliao.platform.messaging import Messaging
from muliao.platform.tasks import Commit, TaskQueue
from muliao.sessions.codec import dump_message, load_messages
from muliao.sessions.conventions import is_stopped
from muliao.sessions.delegation import threads_of_holds_in
from muliao.sessions.events import SessionStopped, StopThread
from muliao.sessions.history import latest_answer, marked_stopped, since_last_response, stopped_results
from muliao.sessions.holds import clear_in
from muliao.sessions.lanes import RUN_TASK, session_lane
from muliao.sessions.mailbox import latest_run
from muliao.sessions.settlement import settle_stopped_in
from muliao.sessions.stopping import StoppableRuns
from muliao.sessions.tables import Message, Session

log = logging.getLogger(__name__)


async def close_history_in(s: AsyncSession, session_id: str) -> None:
    """停止时让会话历史停在不会被续跑的地方（唯一事实源是库里的历史）。运行中被停止时运行已经保存了这些，这里没事可做。
    只看最后一条模型回复起的那一段（history.since_last_response）：没出结果的调用只可能在那里。"""
    history = await since_last_response(s, session_id)
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


# 停止会话的端口（用户停止、逐层停止 thread、删除 agent 的会话都经它）：SessionStopper 实现，用到停止的领域都依赖
# 这个类型，不各写一份签名
StopSession = Callable[[str], Awaitable[Halted]]


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
                t.id for t in await self._queue.finish_pending_in(s, session_lane(session_id), RUN_TASK)
            ]
            cleared = await clear_in(s, session_id)
            settled, discarded = await settle_stopped_in(
                s, self._queue, session, await latest_answer(s, session_id)
            )
            # 被清掉的委派：做这些工作的 thread 一起停下（逐层，由它们自己的停止继续）
            threads = list(dict.fromkeys((await threads_of_holds_in(s, cleared)).values()))
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


class SessionStopper:
    """停止会话的命令（用户停止、逐层停止 thread、删除 agent 的会话）：停下正在进行的运行（运行在退出时自己执行
    SessionHalt，见 sessions.consumer），没有在运行时直接执行 SessionHalt。会话已删除时返回 GONE。

    由正在进行的运行的登记（StoppableRuns）与 SessionHalt 组成，二者都不依赖消费者：在组合根里先于消费者与所有
    用到停止的领域构造，消费者用同一份登记运行。"""

    def __init__(self, runs: StoppableRuns[Commit], halt: SessionHalt):
        self._runs = runs
        self._halt = halt

    async def __call__(self, session_id: str) -> Halted:
        if await self._runs.stop(session_id):
            return Halted.STOPPED
        return await self._halt(session_id)
