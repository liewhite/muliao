"""委派：会话交给另一个 agent、要交回结局的工作（ask_agent，delegate_task(report_back)）。委派领域是流程管理者：委派行
只由它的投递 lane（sub:delegations）创建和修改，它只对事件做出反应、经会话领域的命令交回结局。设计见
docs/architecture.md 第 8 节。

- 发起（工具里）：Threads.dispatch 在一个事务里占用发起方、给 thread 投信、发布 DelegationDispatched；委派领域收到后
  建委派行。同一个委派的 DelegationDispatched 一定先于它的 SessionSettled 到达这条 lane（子会话要先处理那封信件才
  可能结清它）。
- 结局只交回一次（状态离开 running）：thread 结清（SessionSettled：完成、被停止、最终失败、被删除）时经会话领域的
  deliver_in 交回发起方——发起方还停在那次 ask_agent 调用上时作为调用的结果，否则作为事件。
- 发起方被停止（SessionStopped）：被清掉的委派放弃（不再交回），thread 逐层停下。
- 运行中检查：发起方没在等这次调用时（它在等时检查信件交不到模型），给发起方投检查信件。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.api import AgentInfo, agent_in
from muliao.delegations.tables import Delegation, DelegationStatus
from muliao.errors import CorruptState
from muliao.platform.db import Database, utcnow
from muliao.platform.messaging import Delivery, Messaging
from muliao.platform.tables import Task, TaskStatus
from muliao.platform.tasks import Clock, Commit, TaskQueue, no_commit
from muliao.sessions.api import (
    AgentAnswer,
    Halted,
    SessionInfo,
    deliver_in,
    latest_run,
    pending_in,
    post_check_in,
    session_in,
)
from muliao.sessions.events import DelegationDispatched, Ending, SessionSettled, SessionStopped

log = logging.getLogger(__name__)


def thread_source(child_id: str) -> str:
    """thread 交回的事件的来源，也用来判断它是否已有信件在发起方的信箱里排着。"""
    return f"thread:{child_id}"


def thread_name(child: SessionInfo) -> str:
    if child.thread is None:
        raise RuntimeError(f"session {child.id} is not a thread")
    return child.thread


def _result_text(thread: str, agent: str, ending: Ending, text: str) -> str:
    """作为 ask_agent 调用的结果：完成时就是回答本身（发问方问的就是它）。"""
    if ending == Ending.FINISHED:
        return text
    return _event_text(thread, agent, ending, text)


def _event_text(thread: str, agent: str, ending: Ending, text: str) -> str:
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


class Delegations:
    """委派的流程管理者。stop_session 是会话领域停止会话的命令（组合根延迟绑定）。"""

    def __init__(
        self,
        db: Database,
        queue: TaskQueue,
        stop_session: Callable[[str], Awaitable[Halted]],
        clock: Clock = utcnow,
    ):
        self._db = db
        self._queue = queue
        self._stop_session = stop_session
        self._clock = clock

    def subscribe(self, messaging: Messaging) -> None:
        # 一个订阅者一条 lane：委派行只有这一个写者
        messaging.subscribe(DelegationDispatched, "delegations.dispatched", self.on_dispatched, lane=_lane)
        messaging.subscribe(SessionSettled, "delegations.settled", self.on_settled, lane=_lane)
        messaging.subscribe(SessionStopped, "delegations.stopped", self.on_stopped, lane=_lane)

    async def on_dispatched(self, event: DelegationDispatched, _delivery: Delivery) -> Commit:
        async def commit(s: AsyncSession) -> None:
            if await _by_holder(s, event.holder) is None:
                s.add(
                    Delegation(
                        holder=event.holder,
                        owner_session_id=event.owner_session_id,
                        child_session_id=event.child_session_id,
                        call_id=event.call_id,
                        awaited=event.awaited,
                    )
                )
                log.info(
                    "delegation started",
                    extra={
                        "holder": event.holder,
                        "session_id": event.owner_session_id,
                        "thread_session_id": event.child_session_id,
                    },
                )

        return commit

    async def on_settled(self, event: SessionSettled, delivery: Delivery) -> Commit:
        """thread 结清了这些委派：还在进行的，经会话领域交回发起方（按事件 id + 委派幂等），然后记为 settled。"""
        async with self._db.transaction() as s:
            delegations = []
            for holder in event.correlations:
                delegation = await _by_holder(s, holder)
                if delegation is None:
                    # DelegationDispatched 一定先到（同一条 lane、更小的 id）：没有这一行是 bug
                    raise CorruptState(f"thread {event.session_id} settled unknown delegation {holder}")
                if delegation.status == DelegationStatus.RUNNING:
                    delegations.append(delegation)
        agent_name = event.agent_name
        for delegation in delegations:
            async with self._db.transaction() as s:
                delivered = await deliver_in(
                    s,
                    self._queue,
                    delegation.owner_session_id,
                    delegation.holder,
                    key=f"{delivery.message_id}:{delegation.holder}",
                    awaited=delegation.awaited,
                    source=thread_source(event.session_id),
                    event_text=_event_text(event.thread, agent_name, event.ending, event.text),
                    answer=AgentAnswer(
                        text=_result_text(event.thread, agent_name, event.ending, event.text),
                        attachments=list(event.attachments),
                        thread=event.session_id,
                    ),
                )
            log.info(
                "delegation settled",
                extra={
                    "holder": delegation.holder,
                    "session_id": delegation.owner_session_id,
                    "thread_session_id": event.session_id,
                    "ending": event.ending,
                    "delivered_as": delivered,
                },
            )
        holders = [d.holder for d in delegations]

        async def commit(s: AsyncSession) -> None:
            for holder in holders:
                row = await _by_holder(s, holder)
                if row is None or row.status != DelegationStatus.RUNNING:
                    raise RuntimeError(f"delegation {holder} changed while it was being settled")
                row.status, row.settled_at = DelegationStatus.SETTLED, self._clock()

        return commit

    async def on_stopped(self, event: SessionStopped, _delivery: Delivery) -> Commit:
        """发起方被停止：被清掉的委派放弃（不再交回），做这些工作的 thread 一起停下（逐层，由它们自己的停止继续）。"""
        # 占用者对委派领域也是不透明的：被清掉的占用里哪些是委派，看自己的委派行
        if not event.cleared:
            return no_commit
        held = list(event.cleared)
        async with self._db.transaction() as s:
            rows = list(
                (
                    await s.scalars(
                        select(Delegation).where(
                            Delegation.holder.in_(held), Delegation.status == DelegationStatus.RUNNING
                        )
                    )
                ).all()
            )
        children = list(dict.fromkeys(d.child_session_id for d in rows))
        for child in children:
            # thread 可能已被用户删除（不忙时可以删）：停止返回 GONE，照常放弃委派
            await self._stop_session(child)
        if children:
            log.info(
                "delegations abandoned with their session",
                extra={"session_id": event.session_id, "thread_session_ids": children},
            )
        holders = [d.holder for d in rows]

        async def commit(s: AsyncSession) -> None:
            for holder in holders:
                row = await _by_holder(s, holder)
                if row is not None and row.status == DelegationStatus.RUNNING:
                    row.status, row.settled_at = DelegationStatus.ABANDONED, self._clock()

        return commit

    async def check(self) -> int:
        posted = 0
        async with self._db.transaction() as s:
            running = (
                await s.scalars(
                    select(Delegation)
                    .where(Delegation.status == DelegationStatus.RUNNING)
                    .order_by(Delegation.id)
                )
            ).all()
            seen: set[tuple[str, str]] = set()
            for delegation in running:
                key = (delegation.owner_session_id, delegation.child_session_id)
                if key in seen:
                    continue
                seen.add(key)
                if await session_in(s, delegation.owner_session_id) is None:
                    # 发起方已删除（它不忙时才能删，删前委派已结清或放弃；这里只会是交回途中的窗口）
                    continue
                if delegation.call_id is not None and delegation.call_id in await pending_in(
                    s, delegation.owner_session_id
                ):
                    # 发起方停在这次调用上：检查信件要等回答一起才交给模型，没有意义
                    continue
                child = await session_in(s, delegation.child_session_id)
                if child is None:
                    # thread 已删除：它的结清正在交回途中
                    continue
                agent = await agent_in(s, child.agent_id)
                text = check_text(child, agent, await latest_run(s, child.id), self._clock())
                posted += await post_check_in(
                    s,
                    self._queue,
                    delegation.owner_session_id,
                    delegation.holder,
                    thread_source(child.id),
                    text,
                )
        return posted


def _lane(_event: object) -> str:
    return "sub:delegations"


async def _by_holder(s: AsyncSession, holder: str) -> Delegation | None:
    return await s.scalar(select(Delegation).where(Delegation.holder == holder))


def check_text(child: SessionInfo, agent: AgentInfo, run: Task | None, now: datetime) -> str:
    """run 为 None：委派信件还排着，thread 还没有运行。最近一次运行已经结束（委派却还没结清）：thread 停在它自己的
    挂起调用上，或在等它发起的后台任务、委派。"""
    name = thread_name(child)
    stop = f"if it is no longer needed, call stop_thread({name!r})."
    if run is None or run.status in (TaskStatus.PENDING, TaskStatus.DONE, TaskStatus.FAILED):
        if run is not None and run.status == TaskStatus.PENDING and run.attempts:
            state = f"is waiting to run again (attempt {run.attempts} so far)"
        elif run is not None and run.status == TaskStatus.DONE:
            state = (
                "is waiting for work it started (background jobs, other agents) or for answers it asked for"
            )
        else:
            state = "is queued and has not started yet"
        return f"Thread {name!r} ({agent.name}) {state}. Keep waiting, you will get an event with its answer; {stop}"
    if run.started_at is None:
        raise RuntimeError(f"running run {run.id} has no start time")
    minutes = int((now - run.started_at).total_seconds() // 60)
    return (
        f"Thread {name!r} ({agent.name}) is still working on your task ({minutes} min so far).\n"
        f"Keep waiting, you will get an event with its answer; {stop}"
    )
