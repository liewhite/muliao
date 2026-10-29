"""会话忙不忙：别的领域与删除规则问的两个不同问题（docs/architecture.md 第 7 节）。

- 工作状态（SessionActivity.work_state_in：running / waiting / idle，唯一规则）：会话列表、issue 看板、触发器避免重叠触发都看它；
- 删除会不会打断工作（blocking_deletion_in 及按 agent 的版本）：删除会话、删除 agent 的检查。

看哪些 lane、哪些占用是会话领域的实现细节：别的领域经 SessionActivity 问，不拿任务队列。
"""

from collections.abc import Collection, Mapping
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.platform.tasks import TaskQueue
from muliao.sessions.directory import keyed_among, of_agent_in
from muliao.sessions.holds import with_active_among
from muliao.sessions.info import keyed_sessions_among
from muliao.sessions.lanes import keyed_receive_lane, receive_lane, session_lane
from muliao.sessions.mailbox import open_asks_among
from muliao.sessions.origins import Origin
from muliao.sessions.tables import Session


class WorkState(StrEnum):
    """会话此刻在不在处理事情。running 与 waiting 都是「事情还没做完」：等待中的会话结果回来会自动接着做，
    所以除了展示，别的判断（issue 看板的「没人在处理」、触发器避免重叠）都只区分 idle 与否。"""

    # 有排着或正在进行的运行，或有投给它、还没投递的来信
    RUNNING = "running"
    # 运行停了，但在等外面的结果：后台任务、委派出去的工作、到点唤醒，或挂起的提问等用户回答
    WAITING = "waiting"
    # 没有任何进行中的事
    IDLE = "idle"


async def work_states_in(
    s: AsyncSession, queue: TaskQueue, session_ids: Collection[str]
) -> dict[str, WorkState]:
    """一批会话的工作状态，查询次数与会话数无关（会话列表一次要几百个，事务持有全局写锁）：running 有排队或进行中
    的运行、还没投递的来信；waiting 有 active 占用（后台任务、委派、到点唤醒）或还能回答的提问；否则 idle。"""
    lanes: dict[str, tuple[str, ...]] = {x: (session_lane(x), receive_lane(x)) for x in session_ids}
    # 按发送方定位（inbound.SessionKey）投给它、还没投递的来信在按 key 命名的 lane 上
    for x, (origin, origin_ref, agent_id) in (await keyed_among(s, session_ids)).items():
        lanes[x] = (*lanes[x], keyed_receive_lane(origin, origin_ref, agent_id))
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
    出去的工作）。与工作状态（SessionActivity.work_state_in）回答的是不同的问题：挂起的提问没有人在外面做事，不挡删除。"""
    busy = await queue.busy_lanes(s, [session_lane(x) for x in sessions])
    held = await with_active_among(s, list(sessions))
    return [x for x in sessions if session_lane(x) in busy or x in held]


async def sessions_blocking_agent_deletion_in(s: AsyncSession, queue: TaskQueue, agent_id: str) -> list[str]:
    """删除这个 agent 时会被删除、此刻忙着的会话（它的全部会话）。"""
    return await blocking_deletion_in(s, queue, {x.id: x for x in await of_agent_in(s, agent_id)})


class SessionActivity:
    """别的领域问「会话忙不忙」的查询（只读，在调用方的事务里）：看哪些 lane、哪些占用是会话领域的实现细节，不交给
    调用方。两个问题不同：工作状态（会话列表、issue 看板、触发器避免重叠触发），删除会不会打断工作（删除 agent 的
    检查）。"""

    def __init__(self, queue: TaskQueue):
        self._queue = queue

    async def work_state_in(self, s: AsyncSession, session_id: str) -> WorkState:
        return (await work_states_in(s, self._queue, [session_id]))[session_id]

    async def keyed_work_states_in(
        self, s: AsyncSession, origin: Origin, keys: Collection[tuple[str, str]]
    ) -> dict[tuple[str, str], WorkState | None]:
        """按发送方定位的会话（inbound.SessionKey）的工作状态，key 是 (origin_ref, agent id)：会话存在时是它的状态；还没
        建出来、但按 key 投给它的来信还没投递时是 running（信一投就建出会话开始处理）；否则 None。"""
        keyed = await keyed_sessions_among(s, origin, {ref for ref, _ in keys})
        found = {k: keyed[k[0]][k[1]].id for k in keys if k[1] in keyed[k[0]]}
        states = await work_states_in(s, self._queue, list(found.values()))
        pending = await self._queue.busy_lanes(
            s, [keyed_receive_lane(origin, ref, agent) for ref, agent in keys if (ref, agent) not in found]
        )
        return {
            k: states[found[k]]
            if k in found
            else WorkState.RUNNING
            if keyed_receive_lane(origin, *k) in pending
            else None
            for k in keys
        }

    async def blocks_agent_deletion_in(self, s: AsyncSession, agent_id: str) -> bool:
        """删除这个 agent 时会被删除的会话里有忙的（删除 agent 前的检查，与删除时的判断同一条规则）。"""
        return bool(await sessions_blocking_agent_deletion_in(s, self._queue, agent_id))
