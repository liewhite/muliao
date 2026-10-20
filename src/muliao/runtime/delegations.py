"""委派：会话交给另一个 agent、要交回结局的工作（ask_agent，delegate_task(report_back)），是会话拥有的异步工作的一种
（runtime.work）。设计见 docs/architecture.md「委派」。

- 发起：在 thread 的信箱投一封带委派的信件（conversation.Threads.send），委派行记下发起会话、thread、ask_agent 的
  调用 id、用户是否在等发起方。
- 结局只交回一次（settle_in，唯一出口，状态离开 running）：处理委派信件的运行结束（完成或被停止）时随它的收尾；最终
  失败时随队列记下失败（RunHandler.failed_in）；thread 被停止时随停止（Consumer 的停止）；thread 被删除时随删除
  （conversation.delete_sessions）。发起方还停在那次 ask_agent 调用上时，结局作为调用的结果交回（回答信件）；否则
  （report_back，或用户已经跳过了那次调用）作为事件信件。
- 发起方被用户停止时，它发起的委派一起放弃（不再交回），thread 一起停下。
- 运行中检查：发起方没在等这次调用时（它在等时检查信件交不到模型），给发起方投检查信件。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.conventions import Attachment
from muliao.db import Database, utcnow
from muliao.models import (
    Agent,
    Delegation,
    DelegationStatus,
    Mail,
    MailKind,
    MailState,
    Session,
    Task,
    TaskStatus,
)
from muliao.runtime.mailbox import latest_run, post_in
from muliao.runtime.pending import AgentAnswer, pending_in
from muliao.runtime.tasks import Clock, TaskQueue
from muliao.runtime.work import post_check_in

log = logging.getLogger(__name__)


def thread_source(child_id: str) -> str:
    """thread 交回的事件的来源，也用来判断它是否已有信件在发起方的信箱里排着。"""
    return f"thread:{child_id}"


def thread_name(child: Session) -> str:
    if child.thread is None:
        raise RuntimeError(f"session {child.id} is not a thread")
    return child.thread


class Ending(StrEnum):
    FINISHED = "finished"
    STOPPED = "stopped"
    FAILED = "failed"
    DELETED = "deleted"


@dataclass(frozen=True)
class Outcome:
    """委派的结局。text：完成时是 thread 的回答，被停止时是停止前说的话，失败时是错误说明；被删除时为空。"""

    ending: Ending
    text: str
    # thread 用工具挂给用户的文件（conventions.TOOL_ATTACHMENTS），随 ask_agent 的结果显示在发起方的会话里
    attachments: Sequence[Attachment] = ()


async def start_in(
    s: AsyncSession, *, owner_id: str, child_id: str, call_id: str | None, awaited: bool
) -> Delegation:
    delegation = Delegation(
        owner_session_id=owner_id, child_session_id=child_id, call_id=call_id, awaited=awaited
    )
    s.add(delegation)
    await s.flush()
    log.info(
        "delegation started",
        extra={"delegation_id": delegation.id, "session_id": owner_id, "thread_session_id": child_id},
    )
    return delegation


async def for_call_in(s: AsyncSession, owner_id: str, call_id: str) -> Delegation | None:
    """这次 ask_agent 调用已经发起的委派（续跑重新执行这次调用时据此不重复投信）。"""
    return await s.scalar(
        select(Delegation).where(Delegation.owner_session_id == owner_id, Delegation.call_id == call_id)
    )


async def running_for_in(s: AsyncSession, mail: Sequence[Mail]) -> list[Delegation]:
    """这些信件带着的、还没交回结局的委派。"""
    ids = {m.delegation_id for m in mail if m.delegation_id is not None}
    if not ids:
        return []
    rows = await s.scalars(
        select(Delegation)
        .where(Delegation.id.in_(ids), Delegation.status == DelegationStatus.RUNNING)
        .order_by(Delegation.id)
    )
    return list(rows.all())


async def in_session_in(s: AsyncSession, child_id: str, *, consumed_only: bool = False) -> list[Delegation]:
    """交给这个会话、还没交回的委派：它的委派信件还排着，或已经交给了模型（处理中，可能跨几次运行：停在挂起的
    调用上时等后面的运行接着做完）。consumed_only：只要处理中的。"""
    states = [MailState.CONSUMED] if consumed_only else [MailState.QUEUED, MailState.CONSUMED]
    carried = select(Mail.delegation_id).where(
        Mail.session_id == child_id, Mail.delegation_id.is_not(None), Mail.state.in_(states)
    )
    rows = await s.scalars(
        select(Delegation)
        .where(Delegation.id.in_(carried), Delegation.status == DelegationStatus.RUNNING)
        .order_by(Delegation.id)
    )
    return list(rows.all())


async def settle_in(
    s: AsyncSession, queue: TaskQueue, delegation: Delegation, child: Session, agent: Agent, outcome: Outcome
) -> None:
    """在调用方事务里交回一个委派的结局，唯一出口（见模块说明）。"""
    if delegation.status != DelegationStatus.RUNNING:
        raise RuntimeError(
            f"delegation {delegation.id} is {delegation.status}, its outcome was already settled"
        )
    delegation.status, delegation.settled_at = DelegationStatus.SETTLED, utcnow()
    owner, call_id = delegation.owner_session_id, delegation.call_id
    if call_id is not None and call_id in await pending_in(s, owner):
        delivered = MailKind.ANSWER
        answer = AgentAnswer(
            text=_result_text(child, agent, outcome), attachments=list(outcome.attachments), thread=child.id
        )
        await post_in(
            s,
            queue,
            owner,
            kind=MailKind.ANSWER,
            content="",
            answers={call_id: answer.model_dump()},
            awaited=delegation.awaited,
        )
    else:
        delivered = MailKind.EVENT
        await post_in(
            s,
            queue,
            owner,
            kind=MailKind.EVENT,
            source=thread_source(child.id),
            content=_event_text(child, agent, outcome),
            awaited=delegation.awaited,
        )
    log.info(
        "delegation settled",
        extra={
            "delegation_id": delegation.id,
            "session_id": owner,
            "thread_session_id": child.id,
            "ending": outcome.ending,
            "delivered_as": delivered,
        },
    )


async def settle_all_in(
    s: AsyncSession, queue: TaskQueue, delegations: Sequence[Delegation], child: Session, outcome: Outcome
) -> None:
    """thread 上这些委派一起交回同一个结局。"""
    if not delegations:
        return
    agent = await s.get(Agent, child.agent_id)
    if agent is None:
        raise RuntimeError(f"session {child.id} references missing agent {child.agent_id}")
    for delegation in delegations:
        await settle_in(s, queue, delegation, child, agent, outcome)


def _result_text(child: Session, agent: Agent, outcome: Outcome) -> str:
    """作为 ask_agent 调用的结果：完成时就是回答本身（发问方问的就是它）。"""
    if outcome.ending == Ending.FINISHED:
        return outcome.text
    return _event_text(child, agent, outcome)


def _event_text(child: Session, agent: Agent, outcome: Outcome) -> str:
    who = f"Thread {thread_name(child)!r} ({agent.name})"
    match outcome.ending:
        case Ending.FINISHED:
            return f"{who} finished your task. Its answer:\n{outcome.text}"
        case Ending.STOPPED:
            return (
                f"{who} was stopped before it finished. What it said so far:\n{outcome.text or '(nothing)'}"
            )
        case Ending.FAILED:
            return f"{who} failed: {outcome.text}\nYou can ask it again, or delegate to another agent."
        case Ending.DELETED:
            return f"{who} was deleted by the user before it finished."


class Delegations:
    """委派作为会话拥有的异步工作（runtime.work.SessionWork）。stop_session 是消费者停止会话的入口（组合根延迟绑定）。"""

    def __init__(
        self,
        db: Database,
        queue: TaskQueue,
        stop_session: Callable[[str], Awaitable[None]],
        clock: Clock = utcnow,
    ):
        self._db = db
        self._queue = queue
        self._stop_session = stop_session
        self._clock = clock

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
                if delegation.call_id is not None and delegation.call_id in await pending_in(
                    s, delegation.owner_session_id
                ):
                    # 发起方停在这次调用上：检查信件要等回答一起才交给模型，没有意义
                    continue
                child = await s.get(Session, delegation.child_session_id)
                agent = await s.get(Agent, child.agent_id) if child is not None else None
                if child is None or agent is None:
                    raise RuntimeError(f"delegation {delegation.id} references a missing thread or agent")
                text = check_text(child, agent, await latest_run(s, child.id), self._clock())
                posted += await post_check_in(
                    s, self._queue, delegation.owner_session_id, thread_source(child.id), text
                )
        return posted

    async def active_in(self, s: AsyncSession, owner_session_id: str) -> bool:
        return bool(
            await s.scalar(
                select(
                    select(Delegation.id)
                    .where(
                        Delegation.owner_session_id == owner_session_id,
                        Delegation.status == DelegationStatus.RUNNING,
                    )
                    .exists()
                )
            )
        )

    async def halt(self, owner_session_id: str) -> None:
        """发起方被停止：它发起的委派放弃（不再交回），做这些工作的 thread 一起停下（逐层）。"""
        async with self._db.transaction() as s:
            owned = (
                await s.scalars(
                    select(Delegation).where(
                        Delegation.owner_session_id == owner_session_id,
                        Delegation.status == DelegationStatus.RUNNING,
                    )
                )
            ).all()
            for delegation in owned:
                delegation.status, delegation.settled_at = DelegationStatus.ABANDONED, self._clock()
            children = list(dict.fromkeys(d.child_session_id for d in owned))
        if children:
            log.info(
                "delegations abandoned with their session",
                extra={"session_id": owner_session_id, "thread_session_ids": children},
            )
        for child in children:
            await self._stop_session(child)


def check_text(child: Session, agent: Agent, run: Task | None, now: datetime) -> str:
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
