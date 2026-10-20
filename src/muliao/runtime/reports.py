"""委派的结果交回：delegate_task(report_back) 投出的委派信件（带 report_to），处理它的运行结束（完成、被停止或最终
失败）时，把结果作为事件信件投给发起委派的会话；它还在排队或处理它的运行还没结束时，每隔一段时间给发起方一封检查信件
（与后台任务的检查同一个节奏）。

结果信件与运行的结局在同一个事务里：完成或被停止时随运行的收尾（runtime.consumer），最终失败时随队列记下失败
（RunHandler.failed_in）。公开 thread 在父会话删除后仍然保留，那时结果无处可交，记下后跳过。
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.db import Database, utcnow
from muliao.models import Agent, Mail, MailKind, MailState, Session, Task, TaskStatus
from muliao.runtime.conversation import ReportTo, thread_name
from muliao.runtime.mailbox import has_queued_from, post_in
from muliao.runtime.tasks import Clock, TaskQueue

log = logging.getLogger(__name__)


def thread_source(child_id: str) -> str:
    """thread 交回的事件的来源，也用来判断它是否已有事件在队列里等着。"""
    return f"thread:{child_id}"


async def settle_in(s: AsyncSession, queue: TaskQueue, mail: Mail, child: Session, text: str) -> None:
    """在调用方事务里交回一封委派信件的结局（完成、被停止、最终失败），这是唯一的出口：每个委派只交回一次，交回后
    撤掉交回地址，之后不再交回（例如最终失败交回之后，续跑接管它的运行完成时），检查也随之停止（open_reports）。"""
    if mail.report_to is None:
        raise RuntimeError(f"mail {mail.id} has no report address, or it was already settled")
    report_to = ReportTo.model_validate(mail.report_to)
    mail.report_to = None
    log.info(
        "delegation settled",
        extra={"mail_id": mail.id, "session_id": child.id, "parent_session_id": report_to.session_id},
    )
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
        kind=MailKind.EVENT,
        source=thread_source(child.id),
        content=text,
        awaited=report_to.awaited,
    )


def open_reports(parent_id: str | None = None) -> Select[tuple[Mail]]:
    """结果还没交回的委派信件：还在排队，或处理它的运行还没结束。parent_id 给出时只看委派给这个会话的。"""
    open_runs = select(Task.id).where(Task.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]))
    query = select(Mail).where(
        Mail.report_to.is_not(None), (Mail.state == MailState.QUEUED) | Mail.consumed_by.in_(open_runs)
    )
    if parent_id is not None:
        query = query.where(func.json_extract(Mail.report_to, "$.session_id") == parent_id)
    return query.order_by(Mail.id)


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
    """给结果还没交回的委派，向发起方发检查信件。"""

    def __init__(self, db: Database, queue: TaskQueue, clock: Clock = utcnow):
        self._db = db
        self._queue = queue
        self._clock = clock

    async def check(self) -> int:
        """发出的检查信件数。同一个 thread 上一封信件还在发起方的信箱里排队时不再发。"""
        posted = 0
        async with self._db.transaction() as s:
            pending = list((await s.scalars(open_reports())).all())
            for child_id in dict.fromkeys(m.session_id for m in pending):
                mail = [m for m in pending if m.session_id == child_id]
                report_to = ReportTo.model_validate(mail[-1].report_to)
                child = await s.get(Session, child_id)
                if child is None or await s.get(Session, report_to.session_id) is None:
                    continue
                if await has_queued_from(s, report_to.session_id, thread_source(child_id)):
                    continue
                agent = await s.get(Agent, child.agent_id)
                if agent is None:
                    raise RuntimeError(f"session {child.id} references missing agent {child.agent_id}")
                run = await s.get(Task, mail[-1].consumed_by) if mail[-1].consumed_by is not None else None
                await post_in(
                    s,
                    self._queue,
                    report_to.session_id,
                    kind=MailKind.EVENT,
                    source=thread_source(child_id),
                    content=_check_text(child, agent, run, self._clock()),
                    awaited=False,
                )
                posted += 1
        return posted


def _check_text(child: Session, agent: Agent, run: Task | None, now: datetime) -> str:
    """run 为 None：委派信件还在 thread 的信箱里排队，还没有运行处理它。"""
    name = thread_name(child)
    if run is None or run.status == TaskStatus.PENDING:
        state = (
            "is queued and has not started yet"
            if run is None or not run.attempts
            else (f"is waiting to run again (attempt {run.attempts} so far)")
        )
        return (
            f"Thread {name!r} ({agent.name}) {state}. Keep waiting, you will get an event with its answer; "
            f"if it is no longer needed, call stop_thread({name!r})."
        )
    if run.started_at is None:
        raise RuntimeError(f"running run {run.id} has no start time")
    minutes = int((now - run.started_at).total_seconds() // 60)
    return (
        f"Thread {name!r} ({agent.name}) is still working on your task ({minutes} min so far).\n"
        f"If it is no longer needed, call stop_thread({name!r}); otherwise just keep waiting, "
        "you will get an event with its answer."
    )
