"""委派的结果交回：delegate_task(report_back) 开出的 thread 那一轮结束（完成、被停止或最终失败）时，把结果作为
会话事件交给发起委派的会话；它还在排队或运行时，每隔一段时间给发起方一个检查事件（与后台任务的检查同一个节奏）。

结果事件与那一轮的结局在同一个事务里：完成时随这一轮提交（TurnRunner.commit_in），最终失败时随队列记下失败
（TurnHandler.failed_in）。公开 thread 在父会话删除后仍然保留，那时结果无处可交，记下后跳过。
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.db import Database, utcnow
from muliao.models import Agent, ReplyTo, Session, SessionEvent, Task, TaskKind, TaskStatus
from muliao.runtime.conversation import ReportTo, TurnPayload, thread_name
from muliao.runtime.events import post_in
from muliao.runtime.tasks import Clock, TaskQueue

log = logging.getLogger(__name__)


def thread_source(child_id: str) -> str:
    """thread 交回的事件的来源，也用来判断它是否已有事件在队列里等着。"""
    return f"thread:{child_id}"


async def report_in(
    s: AsyncSession, queue: TaskQueue, report_to: ReportTo, child: Session, text: str
) -> None:
    """在调用方事务里把 thread 的结果交给发起方的会话。"""
    if await s.get(Session, report_to.session_id) is None:
        log.info(
            "delegated thread finished after its parent session was deleted, nothing to report to",
            extra={"session_id": child.id, "parent_session_id": report_to.session_id},
        )
        return
    await post_in(
        s,
        queue,
        report_to.session_id,
        source=thread_source(child.id),
        content=text,
        reply_to=report_to.reply_to,
    )


def finished_text(child: Session, agent: Agent, answer: str, *, stopped: bool) -> str:
    who = f"Thread {thread_name(child)!r} ({agent.name})"
    if stopped:
        return f"{who} was stopped before it finished. What it said so far:\n{answer or '(nothing)'}"
    return f"{who} finished your task. Its answer:\n{answer}"


def failed_text(child: Session, agent: Agent, error: str) -> str:
    return (
        f"Thread {thread_name(child)!r} ({agent.name}) failed: {error}\n"
        "The user can retry it from the app, or you can delegate again."
    )


class ReportWatcher:
    """给还在排队或运行、结果要交回的 thread 轮次，向发起方发检查事件。"""

    def __init__(self, db: Database, queue: TaskQueue, clock: Clock = utcnow):
        self._db = db
        self._queue = queue
        self._clock = clock

    async def check(self) -> int:
        """发出的检查事件数。同一个 thread 上一条事件还在队列里等着时不再发。"""
        posted = 0
        async with self._db.transaction() as s:
            open_turns = (
                await s.scalars(
                    select(Task).where(
                        Task.kind == TaskKind.TURN, Task.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING])
                    )
                )
            ).all()
            for turn in open_turns:
                report_to = TurnPayload.of(turn).report_to
                if report_to is None or turn.session_id is None:
                    continue
                child = await s.get(Session, turn.session_id)
                parent = await s.get(Session, report_to.session_id)
                if child is None or parent is None:
                    continue
                waiting = await s.scalar(
                    select(
                        select(SessionEvent.id)
                        .where(
                            SessionEvent.source == thread_source(child.id), SessionEvent.consumed_by.is_(None)
                        )
                        .exists()
                    )
                )
                if waiting:
                    continue
                agent = await s.get(Agent, child.agent_id)
                if agent is None:
                    raise RuntimeError(f"session {child.id} references missing agent {child.agent_id}")
                await post_in(
                    s,
                    self._queue,
                    report_to.session_id,
                    source=thread_source(child.id),
                    content=_check_text(child, agent, turn, self._clock()),
                    reply_to=ReplyTo.NONE,
                )
                posted += 1
        return posted


def _check_text(child: Session, agent: Agent, turn: Task, now: datetime) -> str:
    name = thread_name(child)
    if turn.status == TaskStatus.PENDING:
        state = (
            f"failed temporarily and will retry (attempt {turn.attempts} so far)"
            if turn.attempts
            else "is queued and has not started yet"
        )
        # stop_thread 只能停正在运行的一轮
        return f"Thread {name!r} ({agent.name}) {state}. Keep waiting; you will get an event with its answer."
    else:
        if turn.started_at is None:
            raise RuntimeError(f"running turn {turn.id} has no start time")
        state = (
            f"is still working on your task ({int((now - turn.started_at).total_seconds() // 60)} min so far)"
        )
    return (
        f"Thread {name!r} ({agent.name}) {state}.\n"
        f"If it is no longer needed, call stop_thread({name!r}); otherwise just keep waiting, "
        "you will get an event with its answer."
    )
