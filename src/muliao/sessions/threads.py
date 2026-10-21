"""thread：父会话内对子会话的具名引用，同名即续聊（docs/architecture.md 第 7 节「thread 与结果路由」）。

委派都是往 thread 的信箱投信，thread 由自己的消费者处理：delegate_task 投给公开 thread（用户能看到、能插话）；
ask_agent 投给私有 thread（用户只读），发问方停在这次调用上，结局作为它的结果交回（sessions.delegation）。"""

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.api import AgentInfo
from muliao.errors import InvalidInput
from muliao.platform.tables import TaskStatus
from muliao.platform.tasks import TaskQueue
from muliao.platform.tooling import Origin
from muliao.sessions.activity import WorkState, work_states_in
from muliao.sessions.delegation import delegation_holder, thread_name
from muliao.sessions.history import latest_answer
from muliao.sessions.holds import get_in as get_hold_in
from muliao.sessions.holds import hold_in
from muliao.sessions.inbound import Inbound, NewSession, Received, create_session, receive
from muliao.sessions.kinds import SessionKind, policy_of
from muliao.sessions.mailbox import latest_run
from muliao.sessions.settlement import failure_of
from muliao.sessions.tables import Mail, Session

log = logging.getLogger(__name__)


THREAD_MAX = 100


def validate_thread(name: str) -> str:
    name = name.strip()
    if not name or len(name) > THREAD_MAX:
        raise InvalidInput(f"thread name must be 1-{THREAD_MAX} characters")
    return name


async def threads_of(s: AsyncSession, parent_id: str) -> list[Session]:
    """父会话开出的 thread（含私有的），按创建顺序。"""
    query = select(Session).where(Session.parent_session_id == parent_id).order_by(Session.created_at)
    return list((await s.scalars(query)).all())


@dataclass(frozen=True)
class ThreadState:
    # 工作状态（activity.work_states_in，唯一规则）：waiting 是 thread 在等它自己开出的后台任务、委派或提问的回答，
    # 工作还没做完
    work: WorkState
    # 最近一次运行最终失败时的错误说明，否则为 None
    failure: str | None
    # 会话最近的回答（还没有回复时为空）；work 不是 idle 时它是中间的回答
    answer: str


@dataclass(frozen=True)
class ThreadView:
    """父会话开出的一个 thread（list_threads 看到的）。"""

    session_id: str
    thread: str
    agent_id: str
    kind: SessionKind
    state: ThreadState


class Threads:
    def __init__(self, queue: TaskQueue):
        self._queue = queue

    async def find(self, s: AsyncSession, parent_id: str, thread: str) -> Session | None:
        return await s.scalar(
            select(Session).where(
                Session.parent_session_id == parent_id, Session.thread == validate_thread(thread)
            )
        )

    async def open(
        self, s: AsyncSession, *, parent_id: str, agent: AgentInfo, thread: str, kind: SessionKind
    ) -> Session:
        """取已有 thread 或新建。

        归属在创建时确定：thread 固定属于一个 agent；私有 thread 只能用 ask_agent 续聊，公开 thread 只能用
        delegate_task（公开 thread 由自己的消费者处理、用户也能插话，不能在调用方那一步里直接跑）。
        """
        thread = validate_thread(thread)
        existing = await self.find(s, parent_id, thread)
        if existing is not None:
            if existing.agent_id != agent.id:
                raise InvalidInput(
                    f"thread {thread!r} already belongs to another agent; use a different thread name"
                )
            if existing.kind != kind:
                tool = policy_of(existing.kind).continued_by
                raise InvalidInput(f"thread {thread!r} was started with {tool}; use {tool} to continue it")
            return existing
        return await create_session(
            s,
            origin=Origin.DELEGATION,
            origin_ref=parent_id,
            spec=NewSession(
                agent_id=agent.id, title=thread, parent_session_id=parent_id, thread=thread, kind=kind
            ),
        )

    async def send(
        self, s: AsyncSession, *, parent_id: str, agent: AgentInfo, thread: str, prompt: str
    ) -> Received:
        """不交回结局的委派（delegate_task）：往公开 thread 的信箱投一封用户在等的信（thread 不存在则新建）。"""
        child = await self.open(s, parent_id=parent_id, agent=agent, thread=thread, kind=SessionKind.THREAD)
        inbound = Inbound(
            origin=Origin.DELEGATION, origin_ref=parent_id, content=prompt, session=child.id, awaited=True
        )
        return await receive(s, self._queue, inbound)

    async def dispatch(
        self,
        s: AsyncSession,
        *,
        owner_id: str,
        agent: AgentInfo,
        thread: str,
        prompt: str,
        kind: SessionKind,
        call_id: str | None,
        awaited: bool,
        key: str,
    ) -> Session:
        """要交回结局的委派（ask_agent、delegate_task(report_back)），在调用方的一个事务里：打开或找到 thread，在发起方
        登记占用（带 call_id 时这次调用从此算作已交出），往 thread 投一封以占用者为关联 id 的信件（委派的全部状态，
        结局的交回见 sessions.delegation）。thread 不合用时抛 InvalidInput，什么都没写。按 key 幂等：续跑重新执行这次
        调用时只返回已经投递到的 thread。awaited：用户是否在等发起方的回答。"""
        holder = delegation_holder(owner_id, key)
        if await get_hold_in(s, owner_id, holder) is not None:
            existing = await s.scalar(select(Mail.session_id).where(Mail.correlation == holder).limit(1))
            if existing is None:
                raise RuntimeError(f"delegation {holder} is held but its thread mail is missing")
            child = await s.get(Session, existing)
            if child is None:
                raise RuntimeError(f"delegation {holder} points to a missing thread {existing}")
            return child
        child = await self.open(s, parent_id=owner_id, agent=agent, thread=thread, kind=kind)
        await hold_in(s, owner_id, holder, call_id=call_id, awaited=awaited)
        await receive(
            s,
            self._queue,
            Inbound(
                origin=Origin.DELEGATION,
                origin_ref=owner_id,
                content=prompt,
                session=child.id,
                # 没有人等 thread 的回答：结局交回发起方，由它处理
                awaited=False,
                correlation=holder,
            ),
        )
        log.info(
            "delegation dispatched",
            extra={
                "session_id": owner_id,
                "thread_session_id": child.id,
                "holder": holder,
                "call_id": call_id,
            },
        )
        return child

    async def thread_id_in(self, s: AsyncSession, parent_id: str, thread: str) -> str | None:
        """父会话里名为 thread 的子会话 id；没有这个 thread 时为 None。"""
        child = await self.find(s, parent_id, thread)
        return None if child is None else child.id

    async def list_in(self, s: AsyncSession, parent_id: str) -> list[ThreadView]:
        """父会话开出的 thread（含私有的），按创建顺序，带各自的状态。"""
        children = await threads_of(s, parent_id)
        works = await work_states_in(s, self._queue, [c.id for c in children])
        return [
            ThreadView(
                session_id=child.id,
                thread=thread_name(child),
                agent_id=child.agent_id,
                kind=child.kind,
                state=ThreadState(
                    work=works[child.id],
                    failure=await _failure_in(s, child.id),
                    answer=await latest_answer(s, child.id),
                ),
            )
            for child in children
        ]


async def _failure_in(s: AsyncSession, session_id: str) -> str | None:
    """最近一次运行最终失败时的错误说明（之后又有运行排上就不再算）。"""
    run = await latest_run(s, session_id)
    return failure_of(run) if run is not None and run.status == TaskStatus.FAILED else None
