"""会话忙不忙：别的领域与删除规则问的两个不同问题（docs/architecture.md 第 7 节）。

- 有没有排着或正在做的事（session_has_pending_work_in）：触发器避免重叠触发；
- 删除会不会打断工作（blocking_deletion_in 及按 agent 的版本）：删除会话、删除 agent 的检查。

看哪些 lane、哪些占用是会话领域的实现细节：别的领域经 SessionActivity 问，不拿任务队列。
"""

from collections.abc import Mapping
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.platform.tasks import TaskQueue
from muliao.sessions.holds import has_active_in
from muliao.sessions.inbound import open_asks
from muliao.sessions.lanes import receive_lane, session_lane
from muliao.sessions.tables import Session
from muliao.sessions.threads import owned_threads_in


async def session_has_pending_work_in(s: AsyncSession, queue: TaskQueue, session_id: str) -> bool:
    """会话有没有排着或正在做的事：它的 lane 上有排队或进行中的任务，或有投给它、还没投递的来信（Receive）。触发器
    据此避免重叠触发。与「能不能删除」（blocking_deletion_in：lane 加 active 占用）回答的是不同的问题。"""
    return await queue.lane_busy(s, session_lane(session_id)) or await queue.lane_busy(
        s, receive_lane(session_id)
    )


class WorkState(StrEnum):
    """会话此刻在不在处理事情（给别的领域看，如 issue 看板判断被指派的 agent 是否在处理）。"""

    # 有排着或正在进行的运行，或有投给它、还没投递的来信
    RUNNING = "running"
    # 运行停了，但在等外面的结果：后台任务、委派出去的工作，或挂起的提问等用户回答
    WAITING = "waiting"
    # 没有任何进行中的事
    IDLE = "idle"


async def work_state_in(s: AsyncSession, queue: TaskQueue, session_id: str) -> WorkState:
    if await session_has_pending_work_in(s, queue, session_id):
        return WorkState.RUNNING
    if await has_active_in(s, session_id) or (await open_asks(s, session_id)).answerable:
        return WorkState.WAITING
    return WorkState.IDLE


async def blocking_deletion_in(
    s: AsyncSession, queue: TaskQueue, sessions: Mapping[str, Session]
) -> list[str]:
    """其中忙、因而不能删除的会话：lane 上有排队或运行中的任务（运行、压缩），或有人替它在外面做事（后台任务、委派
    出去的工作）。与「有没有排着的事」（session_has_pending_work_in）回答的是不同的问题。"""
    return [x for x in sessions if await queue.lane_busy(s, session_lane(x)) or await has_active_in(s, x)]


async def sessions_blocking_agent_deletion_in(s: AsyncSession, queue: TaskQueue, agent_id: str) -> list[str]:
    """删除这个 agent 时会被删除、此刻忙着的会话（它的会话与它们的私有 thread）。"""
    roots = list((await s.scalars(select(Session).where(Session.agent_id == agent_id))).all())
    return await blocking_deletion_in(s, queue, await owned_threads_in(s, roots))


async def agent_deletion_blocked_in(s: AsyncSession, queue: TaskQueue, agent_id: str) -> bool:
    """删除这个 agent 时会被删除的会话里有忙的（删除 agent 前的检查，与删除时的判断同一条规则）。"""
    return bool(await sessions_blocking_agent_deletion_in(s, queue, agent_id))


class SessionActivity:
    """别的领域问「会话忙不忙」的查询（只读，在调用方的事务里）：看哪些 lane、哪些占用是会话领域的实现细节，不交给
    调用方。两个问题不同：有没有排着或正在做的事（触发器避免重叠触发），删除会不会打断工作（删除 agent 的检查）。"""

    def __init__(self, queue: TaskQueue):
        self._queue = queue

    async def has_pending_work_in(self, s: AsyncSession, session_id: str) -> bool:
        return await session_has_pending_work_in(s, self._queue, session_id)

    async def work_state_in(self, s: AsyncSession, session_id: str) -> WorkState:
        return await work_state_in(s, self._queue, session_id)

    async def blocks_agent_deletion_in(self, s: AsyncSession, agent_id: str) -> bool:
        return await agent_deletion_blocked_in(s, self._queue, agent_id)
