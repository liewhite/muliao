"""thread：父会话内对子会话的具名引用，同名即续聊（docs/architecture.md 第 7 节「thread 与结果路由」）。

委派都是往 thread 的信箱投信，thread 由自己的消费者处理：delegate_task 投给公开 thread（用户能看到、能插话）；
ask_agent 投给私有 thread（用户只读），发问方停在这次调用上，结局作为它的结果交回（sessions.delegation）。"""

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.api import AgentInfo
from muliao.errors import InvalidInput
from muliao.platform.tasks import TaskQueue
from muliao.sessions.activity import WorkState, work_states_in
from muliao.sessions.delegation import delegation_holder, thread_name, threads_of_holds_in
from muliao.sessions.delegations import record_in
from muliao.sessions.directory import thread_in, threads_named_in, threads_of
from muliao.sessions.history import latest_answers_among
from muliao.sessions.holds import get_in as get_hold_in
from muliao.sessions.holds import hold_in
from muliao.sessions.inbound import Inbound, NewSession, Received, create_session, receive
from muliao.sessions.kinds import SessionKind, policy_of
from muliao.sessions.origins import Origin
from muliao.sessions.runs import latest_runs_among, recorded_failure
from muliao.sessions.tables import Session
from muliao.sessions.thread_calls import record_call_in

log = logging.getLogger(__name__)


THREAD_MAX = 100


def validate_thread(name: str) -> str:
    name = name.strip()
    if not name or len(name) > THREAD_MAX:
        raise InvalidInput(f"thread name must be 1-{THREAD_MAX} characters")
    return name


@dataclass(frozen=True)
class ThreadProgress:
    # 工作状态（activity.work_states_in，唯一规则）：waiting 是 thread 在等它自己开出的后台任务、委派或提问的回答，
    # 工作还没做完
    state: WorkState
    # 最近一次运行最终失败时的错误说明，否则为 None；与 state 无关（失败后它开出的后台任务可能还在跑：waiting）
    failure: str | None
    # 会话最近的回答（还没有回复时为空）；state 不是 idle 时它是中间的回答
    answer: str


@dataclass(frozen=True)
class ThreadView:
    """父会话开出的一个 thread（list_threads 看到的）。"""

    session_id: str
    thread: str
    agent_id: str
    kind: SessionKind
    progress: ThreadProgress


class Threads:
    def __init__(self, queue: TaskQueue):
        self._queue = queue

    async def find(self, s: AsyncSession, parent_id: str, agent_id: str, thread: str) -> Session | None:
        """thread 由（父会话, agent, 名字）确定：给不同的 agent 开同名 thread 是两个 thread。"""
        return await thread_in(s, parent_id, agent_id, validate_thread(thread))

    async def named_in(self, s: AsyncSession, parent_id: str, thread: str) -> list[Session]:
        """父会话里叫这个名字的 thread（每个 agent 至多一个），按创建顺序。"""
        return await threads_named_in(s, parent_id, validate_thread(thread))

    async def open(
        self, s: AsyncSession, *, parent_id: str, agent: AgentInfo, thread: str, kind: SessionKind
    ) -> Session:
        """取已有 thread 或新建。

        thread 按（父会话, agent, 名字）区分；私有 thread 只能用 ask_agent 续聊，公开 thread 只能用
        delegate_task（公开 thread 由自己的消费者处理、用户也能插话，不能在调用方那一步里直接跑）。
        """
        thread = validate_thread(thread)
        existing = await self.find(s, parent_id, agent.id, thread)
        if existing is not None:
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
        self, s: AsyncSession, *, parent_id: str, agent: AgentInfo, thread: str, prompt: str, call_id: str
    ) -> Received:
        """不交回结局的委派（delegate_task）：往公开 thread 的信箱投一封用户在等的信（thread 不存在则新建），记下是这次
        调用（call_id）开出或续写了它（sessions.thread_calls）。"""
        child = await self.open(s, parent_id=parent_id, agent=agent, thread=thread, kind=SessionKind.THREAD)
        await record_call_in(s, parent_id, call_id, child.id)
        inbound = Inbound(
            origin=Origin.DELEGATION, origin_ref=parent_id, content=prompt, session=child.id, awaited=True
        )
        return await receive(s, self._queue, inbound)

    async def dispatch(
        self,
        s: AsyncSession,
        *,
        parent_id: str,
        agent: AgentInfo,
        thread: str,
        prompt: str,
        kind: SessionKind,
        call_id: str,
        suspends: bool,
        awaited: bool,
        check_seconds: float | None = None,
        timeout_seconds: float | None = None,
    ) -> Session:
        """要交回结局的委派（ask_agent、delegate_task(report_back)），在调用方的一个事务里：打开或找到 thread，在发起方
        登记占用（suspends 时这次调用从此算作已交出），往 thread 投一封以占用者为关联 id 的信件（委派的全部状态，结局的
        交回见 sessions.delegation），并记下这次调用开出或续写了这个 thread（sessions.thread_calls）。thread 不合用时抛
        InvalidInput，什么都没写。call_id：发出它的工具调用，按它幂等：续跑重新执行这次调用时只返回已经投递到的
        thread。suspends：这次调用挂起、等结局作为它的结果（ask_agent），占用带上 call_id。awaited：用户是否在等发起方
        的回答。check_seconds：并发等待时的运行中检查间隔；timeout_seconds：阻塞等待的超时（到期停掉 thread，
        ThreadChecks）。"""
        holder = delegation_holder(parent_id, call_id)
        if await get_hold_in(s, parent_id, holder) is not None:
            existing = (await threads_of_holds_in(s, [holder])).get(holder)
            if existing is None:
                raise RuntimeError(f"delegation {holder} is held but its thread is gone")
            child = await s.get(Session, existing)
            if child is None:
                raise RuntimeError(f"delegation {holder} points to a missing thread {existing}")
            return child
        child = await self.open(s, parent_id=parent_id, agent=agent, thread=thread, kind=kind)
        await hold_in(s, parent_id, holder, call_id=call_id if suspends else None, awaited=awaited)
        await record_call_in(s, parent_id, call_id, child.id)
        await record_in(
            s,
            holder=holder,
            parent_id=parent_id,
            thread_id=child.id,
            check_seconds=check_seconds,
            timeout_seconds=timeout_seconds,
        )
        await receive(
            s,
            self._queue,
            Inbound(
                origin=Origin.DELEGATION,
                origin_ref=parent_id,
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
                "session_id": parent_id,
                "thread_session_id": child.id,
                "holder": holder,
                "call_id": call_id,
            },
        )
        return child

    async def list_in(self, s: AsyncSession, parent_id: str) -> list[ThreadView]:
        """父会话开出的 thread（含私有的），按创建顺序，带各自的状态。"""
        children = await threads_of(s, parent_id)
        ids = [c.id for c in children]
        states = await work_states_in(s, self._queue, ids)
        runs = await latest_runs_among(s, self._queue, ids)
        answers = await latest_answers_among(s, ids)
        return [
            ThreadView(
                session_id=child.id,
                thread=thread_name(child),
                agent_id=child.agent_id,
                kind=child.kind,
                progress=ThreadProgress(
                    state=states[child.id],
                    failure=recorded_failure(runs.get(child.id)),
                    answer=answers[child.id],
                ),
            )
            for child in children
        ]
