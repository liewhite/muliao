"""委派：会话交给另一个 agent、要交回结局的工作（ask_agent，delegate_task(report_back)）。整个生命周期在会话领域里，
由两个对称的事务完成（docs/architecture.md 第 8 节）。委派的唯一主体是发出它的调用记下的那一行（sessions.thread_calls），
别处只引用它的 id：

- 发出（Delegations.dispatch）：一个事务里记下委派行（按发起会话与工具调用 id 幂等），在发起方登记引用它的占用（带 call_id
  与「用户是否在等发起方」），往 thread 投一封引用它的信件。
- 交回（hand_back_in，由 thread 结清时调用：运行做完、最终失败、被停止、被删除）：同一个事务里经 deliver_in 把结局
  交给发起方并释放占用——发起方还停在那次 ask_agent 调用上时作为调用的结果，否则作为事件。只交回占用还 active 的：
  发起方已被停止（占用 cleared）、已删除（行与占用随它删除）的没有可交回的。
- 发起方被停止：停止事务清掉占用，并给做这些工作的 thread 各排一个停止任务（逐层由它们自己的停止继续）。
- 运行中检查（ThreadChecks）：并发等待的（report_back）按 heartbeat_seconds 给发起方投检查信件；阻塞等待的
  （ask_agent）不设超时，运行本身有界。

委派的进行状态只有发起方的占用（active / released / cleared）一处；发起参数（thread、心跳间隔）记在委派行上。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agent_runtime.api import Attachment
from muliao.agents.api import AgentInfo, agent_in
from muliao.platform.db import Database, utcnow
from muliao.platform.tasks import Clock, TaskQueue, TaskView
from muliao.sessions.activity import WorkState, work_states_in
from muliao.sessions.answers import AgentAnswer, AgentAnswered
from muliao.sessions.directory import among_in
from muliao.sessions.holds import hold_delegation_in
from muliao.sessions.inbound import FromParent, Inbound, receive
from muliao.sessions.runs import latest_runs_among
from muliao.sessions.sources import thread_source
from muliao.sessions.tables import Session
from muliao.sessions.thread_calls import Owed, call_in, owed_in, record_call_in
from muliao.sessions.threads import Threads, thread_name
from muliao.sessions.work import CheckBeats, deliver_in, post_check_in

log = logging.getLogger(__name__)


class Ending(StrEnum):
    """关联（委派来的工作）的结局。"""

    FINISHED = "finished"
    STOPPED = "stopped"
    FAILED = "failed"
    DELETED = "deleted"


class Delegations:
    """委派的发出（交回是 hand_back_in）。"""

    def __init__(self, queue: TaskQueue, threads: Threads):
        self._queue = queue
        self._threads = threads

    async def dispatch(
        self,
        s: AsyncSession,
        *,
        parent_id: str,
        agent: AgentInfo,
        thread: str,
        prompt: str,
        call_id: str,
        suspends: bool,
        awaited: bool,
        check_seconds: float | None = None,
    ) -> Session:
        """在调用方的一个事务里：打开或找到 thread，记下这项委派（sessions.thread_calls 的一行：这次调用开出或续写了
        这个 thread），在发起方登记引用它的占用（suspends 时这次调用从此算作已交出），往 thread 投一封引用它的信件
        （结局的交回见 hand_back_in）。thread 名不合法时抛 InvalidInput，什么都没写。call_id：发出它的工具调用，按它
        幂等：续跑重新执行这次调用时只返回已经投递到的 thread。suspends：这次调用挂起、等结局作为它的结果（ask_agent），
        占用带上 call_id。awaited：用户是否在等发起方的回答。check_seconds：并发等待时的运行中检查间隔（阻塞等待的为
        None，不设超时）。"""
        if (existing := await call_in(s, parent_id, call_id)) is not None:
            # 委派行、占用与信件在同一个事务里写下：有行就是已经发出过。thread 被删除后行随之删除，这时重放（发出已提交、
            # 那一步还没保存，期间 thread 跑完又被删掉，极少见）会重新发出一次
            child = await s.get(Session, existing.thread_id)
            if child is None:
                raise RuntimeError(
                    f"delegation {existing.id} points to a missing thread {existing.thread_id}"
                )
            return child
        child = await self._threads.open(s, parent_id=parent_id, agent=agent, thread=thread)
        delegation = await record_call_in(s, parent_id, call_id, child.id, check_seconds=check_seconds)
        await hold_delegation_in(
            s, parent_id, delegation.id, call_id=call_id if suspends else None, awaited=awaited
        )
        await receive(
            s,
            self._queue,
            Inbound(
                sender=FromParent(parent_id),
                content=prompt,
                session=child.id,
                # 没有人等 thread 的回答：结局交回发起方，由它处理
                awaited=False,
                delegation=delegation.id,
            ),
        )
        log.info(
            "delegation dispatched",
            extra={
                "session_id": parent_id,
                "thread_session_id": child.id,
                "delegation": delegation.id,
                "call_id": call_id,
            },
        )
        return child


def _result_text(thread: str, agent: str, ending: Ending, text: str) -> str:
    """作为 ask_agent 调用的结果：完成时就是回答本身（发问方问的就是它）。"""
    if ending == Ending.FINISHED:
        return text
    return event_text(thread, agent, ending, text)


def event_text(thread: str, agent: str, ending: Ending, text: str) -> str:
    who = f"Thread {thread!r} ({agent})"
    match ending:
        case Ending.FINISHED:
            return f"{who} finished your task. Its answer:\n{text}"
        case Ending.STOPPED:
            return f"{who} was stopped before it finished. What it said so far:\n{text or '(nothing)'}"
        case Ending.FAILED:
            return f"{who} failed: {text}\nYou can ask it again, or delegate to another agent."
        case Ending.DELETED:
            return f"{who} was deleted by the user before it finished."


async def hand_back_in(
    s: AsyncSession,
    queue: TaskQueue,
    child: Session,
    owed: Sequence[Owed],
    ending: Ending,
    text: str,
    attachments: Sequence[Attachment],
) -> None:
    """thread 结清了这些还没结清的委派（sessions.settlement）：在同一个事务里把结局经发起方引用它们的占用交回
    （deliver_in，唯一出口），交回即释放占用、即结清，所以每项委派只交回一次。"""
    if not owed:
        return
    name = thread_name(child)
    agent = await agent_in(s, child.agent_id)
    for o in owed:
        delivered = await deliver_in(
            s,
            queue,
            o.hold,
            source=thread_source(child.id),
            event_text=event_text(name, agent.name, ending, text),
            answer=AgentAnswered.of(
                AgentAnswer(text=_result_text(name, agent.name, ending, text), attachments=list(attachments))
            ),
        )
        log.info(
            "delegation handed back",
            extra={
                "delegation": o.delegation.id,
                "session_id": o.delegation.parent_id,
                "thread_session_id": child.id,
                "ending": ending,
                "delivered_as": delivered,
            },
        )


class ThreadChecks:
    """委派的运行中检查（WorkWatcher 的一种工作，sessions.work），按委派行记的发起参数（sessions.thread_calls），只看
    还没结清（发起方占用还 active）、并发等待的委派（delegate_task(report_back)，带 check_seconds）：每过一个间隔给
    发起方投一封检查信件。阻塞等待的（ask_agent）没有检查也没有超时：发问方停在调用上一直等到结局（运行本身有界）。"""

    def __init__(self, db: Database, queue: TaskQueue, clock: Clock = utcnow):
        self._db = db
        self._queue = queue
        self._clock = clock
        # 并发委派（按委派行 id）上一次投检查的时刻（还没投过的以登记时刻算）
        self._beats: CheckBeats[int] = CheckBeats()

    async def check(self) -> int:
        posted = 0
        now = self._clock()
        async with self._db.transaction() as s:
            reporting = [
                (o, interval)
                for o in await owed_in(s)
                if (interval := o.delegation.check_seconds) is not None
            ]
            self._beats.keep({o.delegation.id for o, _ in reporting})
            # (发起会话, thread) -> 到期的委派与它的间隔
            due: dict[tuple[str, str], tuple[Owed, float]] = {}
            for o, interval in reporting:
                d = o.delegation
                if not self._beats.due(d.id, o.hold.created_at, interval, now):
                    continue
                self._beats.checked(d.id, now)
                # 同一个发起方对同一个 thread 的几项委派同时到期，检查一次
                due.setdefault((d.parent_id, d.thread_id), (o, interval))
            child_ids = {child_id for _, child_id in due}
            states = await work_states_in(s, self._queue, child_ids)
            runs = await latest_runs_among(s, self._queue, child_ids)
            children = await among_in(s, child_ids)
            agents: dict[str, AgentInfo] = {}
            for (_, child_id), (o, interval) in due.items():
                child = children.get(child_id)
                if child is None:
                    # 不变量断言：thread 删除时委派行随之删除，不会给出已删除的 thread
                    raise RuntimeError(f"delegation {o.delegation.id} points to a missing thread {child_id}")
                if child.agent_id not in agents:
                    agents[child.agent_id] = await agent_in(s, child.agent_id)
                text = check_text(
                    child, agents[child.agent_id], states[child_id], runs.get(child_id), now, interval
                )
                posted += await post_check_in(s, self._queue, o.hold, thread_source(child.id), text)
        return posted


def check_text(
    child: Session,
    agent: AgentInfo,
    state: WorkState,
    run: TaskView | None,
    now: datetime,
    check_seconds: float,
) -> str:
    """还没结清的委派的检查信件，按 thread 的工作状态（activity.work_states_in）说；run 是它最近一次运行（还没有
    运行时为 None），只用来补充细节：在跑多久了、在等重试，还是还没开始处理。check_seconds：这项委派的检查间隔。"""
    name = thread_name(child)
    who = f"Thread {name!r} ({agent.name})"
    keep = (
        "Keep waiting (if nothing needs doing, reply with just '…'): you will get another check-in in "
        f"{check_seconds:g} seconds and an event with its answer; list_threads shows its latest reply. If it is "
        f"no longer needed, call stop_thread(thread={name!r}, agent={agent.name!r})."
    )
    match state:
        case WorkState.WAITING:
            doing = (
                "is waiting for work it started (background jobs, other agents), a wake-up it scheduled, or "
                "answers it asked for"
            )
        case WorkState.RUNNING if run is not None and run.running:
            if run.started_at is None:
                raise RuntimeError(f"running run {run.id} has no start time")
            minutes, seconds = divmod(int((now - run.started_at).total_seconds()), 60)
            return f"{who} is still working on your task ({minutes}m{seconds:02d}s so far).\n{keep}"
        case WorkState.RUNNING if run is not None and run.pending and run.attempts:
            doing = f"is waiting to run again (attempt {run.attempts} so far)"
        case WorkState.IDLE if run is not None and run.failed:
            # 最终失败的收尾钩子自己出错时，结清与唤醒随收尾一起回滚：委派不会自己结束，如实说，让发起方能自己停
            return (
                f"{who} failed on its last run and will not continue on its own; its failure was not handed back to "
                f"you. Call stop_thread(thread={name!r}, agent={agent.name!r}) to settle it."
            )
        case WorkState.RUNNING | WorkState.IDLE:
            doing = "has your task queued and has not started on it yet"
    return f"{who} {doing}. {keep}"
