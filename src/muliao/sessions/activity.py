"""会话忙不忙：别的领域与删除规则问的两个不同问题（docs/architecture.md 第 7 节）。

- 工作状态（SessionActivity.work_state_in：running / waiting / idle，唯一规则）：会话列表、issue 看板都看它；触发器避免重叠触发
  问投递时拿到的句柄（recipient_in，归属表第 7 行）；
- 删除会不会打断工作（blocking_deletion_in 及按 agent 的版本）：删除会话、删除 agent 的检查。

看哪些 lane、哪些占用是会话领域的实现细节：别的领域经 SessionActivity 问，不拿任务队列。
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import assert_never

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.platform.tasks import TaskQueue
from muliao.sessions.directory import among_in, keyed_among, keyed_rows_among, of_agent_in
from muliao.sessions.events import Recipient, ToKeyed, ToSender, ToSession
from muliao.sessions.holds import active_among, with_active_among
from muliao.sessions.lanes import keyed_receive_lane, receive_lane, session_lane
from muliao.sessions.mailbox import open_asks_among
from muliao.sessions.origins import Origin
from muliao.sessions.questions import Question
from muliao.sessions.tables import Session, SessionHold


class WorkState(StrEnum):
    """会话此刻在不在处理事情。running 与 waiting 都是「事情还没做完」：等待中的会话结果回来会自动接着做，
    所以除了展示，别的判断（issue 看板的「没人在处理」、触发器避免重叠）都只区分 idle 与否。"""

    # 有排着或正在进行的运行，或有投给它、还没投递的来信
    RUNNING = "running"
    # 运行停了，但在等外面的结果：后台任务、委派出去的工作、到点唤醒，或挂起的提问、等批准的调用等用户
    WAITING = "waiting"
    # 没有任何进行中的事
    IDLE = "idle"


@dataclass(frozen=True)
class Waiting:
    """会话在等的东西（waiting 的唯一定义）：active 占用（后台任务、委派出去的工作、到点唤醒），与还能回答的提问、
    还能批准的调用。会话页据此列出等待项（sessions.waits）。"""

    # 按登记先后
    holds: tuple[SessionHold, ...]
    # 提问的工具调用 id -> 问题
    asks: Mapping[str, list[Question]]
    # 等批准的工具调用 id -> 工具名
    approvals: Mapping[str, str]

    def __bool__(self) -> bool:
        return bool(self.holds or self.asks or self.approvals)


async def waiting_among(s: AsyncSession, session_ids: Collection[str]) -> dict[str, Waiting]:
    """一批会话各自在等的东西，查询次数与会话数无关。"""
    held = await active_among(s, session_ids)
    asks = await open_asks_among(s, session_ids)
    return {x: Waiting(tuple(held.get(x, ())), asks[x].answerable, asks[x].approvable) for x in session_ids}


async def work_states_in(
    s: AsyncSession, queue: TaskQueue, session_ids: Collection[str]
) -> dict[str, WorkState]:
    """一批会话的工作状态，查询次数与会话数无关（会话列表一次要几百个，事务持有全局写锁）：running 有排队或进行中
    的运行、还没投递的来信；waiting 有 active 占用（后台任务、委派、到点唤醒）或还能回答的提问、还能批准的调用；
    否则 idle。"""
    lanes: dict[str, tuple[str, ...]] = {x: (session_lane(x), receive_lane(x)) for x in session_ids}
    # 按发送方定位（events.SessionKey）投给它、还没投递的来信在按 key 命名的 lane 上
    for x, (origin, origin_ref, agent_id) in (await keyed_among(s, session_ids)).items():
        lanes[x] = (*lanes[x], keyed_receive_lane(origin, origin_ref, agent_id))
    busy = await queue.busy_lanes(s, [lane for pair in lanes.values() for lane in pair])
    waiting = await waiting_among(s, session_ids)
    return {
        x: WorkState.RUNNING
        if busy.intersection(lanes[x])
        else WorkState.WAITING
        if waiting[x]
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


@dataclass(frozen=True)
class RecipientState:
    """一个收件方（events.Recipient）现在的工作状态，和它对应的会话：一个会话的、按 key 定位的是它现在的那个会话（还没建出来、
    已删除为 None）；一个发送方全部会话的，是不空闲的那个（先取 running），全都空闲为 None。防重叠只看 state；session_id 给通知与界面指到会话。

    一个发送方全部会话的算不上「投给还没建出来的会话、还在路上的来信」：不知道投给哪个 agent，按 key 的 lane 无从问起；那只是
    投递前的毫秒级窗口。"""

    state: WorkState
    session_id: str | None


class SessionActivity:
    """别的领域问「会话忙不忙」的查询（只读，在调用方的事务里）：看哪些 lane、哪些占用是会话领域的实现细节，不交给
    调用方。两个问题不同：工作状态（会话列表、issue 看板、触发器避免重叠触发），删除会不会打断工作（删除 agent 的
    检查）。"""

    def __init__(self, queue: TaskQueue):
        self._queue = queue

    async def work_state_in(self, s: AsyncSession, session_id: str) -> WorkState:
        return (await work_states_in(s, self._queue, [session_id]))[session_id]

    async def recipient_in(self, s: AsyncSession, recipient: Recipient) -> RecipientState:
        """投递时拿到的句柄（inbound.Accepted.recipient）现在忙不忙，以及它对应的会话（见 RecipientState）。"""
        return (await self.recipients_in(s, [recipient]))[0]

    async def recipients_in(self, s: AsyncSession, recipients: Sequence[Recipient]) -> list[RecipientState]:
        """一批句柄的 recipient_in，按顺序；查询次数与句柄数无关（触发器列表一次要几百个，事务持有全局写锁）。"""
        direct = {r.session_id for r in recipients if isinstance(r, ToSession)}
        existing = set(await among_in(s, direct)) if direct else set()
        # 按 key 与按发送方的：每种来源一次查出这些发送方开出的会话，(发送方身份, agent) -> 会话
        refs: dict[Origin, set[str]] = {}
        for r in recipients:
            if isinstance(r, ToKeyed | ToSender):
                refs.setdefault(r.origin, set()).add(r.origin_ref)
        keyed = {origin: await keyed_rows_among(s, origin, list(rs)) for origin, rs in refs.items()}
        states = await work_states_in(
            s, self._queue, list(direct | {x.id for rows in keyed.values() for x in rows.values()})
        )
        # 按 key 定位、会话还没建出来的：信还排着就算 running（信一投就建出会话开始处理）
        unbuilt = [
            keyed_receive_lane(r.origin, r.origin_ref, r.agent_id)
            for r in recipients
            if isinstance(r, ToKeyed) and (r.origin_ref, r.agent_id) not in keyed[r.origin]
        ]
        pending = await self._queue.busy_lanes(s, unbuilt) if unbuilt else set()

        def state_of(recipient: Recipient) -> RecipientState:
            match recipient:
                case ToSession(session_id=session_id):
                    return RecipientState(states[session_id], session_id if session_id in existing else None)
                case ToKeyed(origin=origin, origin_ref=ref, agent_id=agent_id):
                    session = keyed[origin].get((ref, agent_id))
                    if session is not None:
                        return RecipientState(states[session.id], session.id)
                    lane = keyed_receive_lane(origin, ref, agent_id)
                    return RecipientState(WorkState.RUNNING if lane in pending else WorkState.IDLE, None)
                case ToSender(origin=origin, origin_ref=ref):
                    ids = [x.id for (r, _), x in keyed[origin].items() if r == ref]
                    for wanted in (WorkState.RUNNING, WorkState.WAITING):
                        if busy := [x for x in ids if states[x] == wanted]:
                            return RecipientState(wanted, busy[0])
                    return RecipientState(WorkState.IDLE, None)
                case other:
                    assert_never(other)

        return [state_of(r) for r in recipients]

    async def keyed_work_states_in(
        self, s: AsyncSession, origin: Origin, keys: Collection[tuple[str, str]]
    ) -> dict[tuple[str, str], WorkState | None]:
        """按发送方定位的会话（events.SessionKey）的工作状态，key 是 (origin_ref, agent id)：会话存在时是它的状态；还没
        建出来、但按 key 投给它的来信还没投递时是 running；否则 None（与 recipients_in 的 ToKeyed 同一条规则）。"""
        keys = list(keys)
        found = await self.recipients_in(
            s, [ToKeyed(origin=origin, origin_ref=ref, agent_id=agent) for ref, agent in keys]
        )
        return {
            k: None if r.session_id is None and r.state == WorkState.IDLE else r.state
            for k, r in zip(keys, found, strict=True)
        }

    async def blocks_agent_deletion_in(self, s: AsyncSession, agent_id: str) -> bool:
        """删除这个 agent 时会被删除的会话里有忙的（删除 agent 前的检查，与删除时的判断同一条规则）。"""
        return bool(await sessions_blocking_agent_deletion_in(s, self._queue, agent_id))
