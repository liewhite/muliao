"""会话忙不忙：别的领域与删除规则问的两个不同问题（docs/architecture.md 第 7 节）。

- 工作状态（work_state_in：running / waiting / idle，唯一规则）：会话列表、issue 看板、触发器避免重叠触发都看它；
- 删除会不会打断工作（blocking_deletion_in 及按 agent 的版本）：删除会话、删除 agent 的检查。

看哪些 lane、哪些占用是会话领域的实现细节：别的领域经 SessionActivity 问，不拿任务队列。
"""

from collections.abc import Collection, Mapping
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.platform.tasks import TaskQueue
from muliao.sessions.holds import has_active_in, with_active_among
from muliao.sessions.inbound import open_asks_among
from muliao.sessions.lanes import receive_lane, session_lane
from muliao.sessions.tables import Session
from muliao.sessions.threads import owned_threads_in


class WorkState(StrEnum):
    """会话此刻在不在处理事情。running 与 waiting 都是「事情还没做完」：等待中的会话结果回来会自动接着做，
    所以除了展示，别的判断（issue 看板的「没人在处理」、触发器避免重叠）都只区分 idle 与否。"""

    # 有排着或正在进行的运行，或有投给它、还没投递的来信
    RUNNING = "running"
    # 运行停了，但在等外面的结果：后台任务、委派出去的工作，或挂起的提问等用户回答
    WAITING = "waiting"
    # 没有任何进行中的事
    IDLE = "idle"


async def work_state_in(s: AsyncSession, queue: TaskQueue, session_id: str) -> WorkState:
    return (await work_states_in(s, queue, [session_id]))[session_id]


async def work_states_in(
    s: AsyncSession, queue: TaskQueue, session_ids: Collection[str]
) -> dict[str, WorkState]:
    """一批会话的工作状态，查询次数与会话数无关（会话列表一次要几百个，事务持有全局写锁）：running 有排队或进行中
    的运行、还没投递的来信；waiting 有 active 占用（后台任务、委派）或还能回答的提问；否则 idle。"""
    lanes = {x: (session_lane(x), receive_lane(x)) for x in session_ids}
    busy = await queue.busy_lanes(s, [lane for pair in lanes.values() for lane in pair])
    held = await with_active_among(s, session_ids)
    asks = await open_asks_among(s, session_ids)
    return {
        x: WorkState.RUNNING
        if busy.intersection(lanes[x])
        else WorkState.WAITING
        if x in held or asks[x].answerable
        else WorkState.IDLE
        for x in session_ids
    }


async def blocking_deletion_in(
    s: AsyncSession, queue: TaskQueue, sessions: Mapping[str, Session]
) -> list[str]:
    """其中忙、因而不能删除的会话：lane 上有排队或运行中的任务（运行、压缩），或有人替它在外面做事（后台任务、委派
    出去的工作）。与工作状态（work_state_in）回答的是不同的问题：挂起的提问没有人在外面做事，不挡删除。"""
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
    调用方。两个问题不同：工作状态（会话列表、issue 看板、触发器避免重叠触发），删除会不会打断工作（删除 agent 的
    检查）。"""

    def __init__(self, queue: TaskQueue):
        self._queue = queue

    async def work_state_in(self, s: AsyncSession, session_id: str) -> WorkState:
        return await work_state_in(s, self._queue, session_id)

    async def blocks_agent_deletion_in(self, s: AsyncSession, agent_id: str) -> bool:
        return await agent_deletion_blocked_in(s, self._queue, agent_id)
