"""委派：会话交给另一个 agent、要交回结局的工作（ask_agent，delegate_task(report_back)）。整个生命周期在会话领域里，
由两个对称的事务完成（docs/architecture.md 第 8 节）：

- 发出（Threads.dispatch）：一个事务里在发起方登记占用（holder 由工具调用 id 确定性派生，带 call_id 与「用户是否在等
  发起方」），往 thread 投一封以 holder 为关联 id 的信件。
- 交回（hand_back_in，由 thread 结清关联时调用：运行做完、最终失败、被停止、被删除）：同一个事务里经 deliver_in 把
  结局交给发起方并释放占用——发起方还停在那次 ask_agent 调用上时作为调用的结果，否则作为事件；发起方已被停止
  （占用 cleared）不投，已删除（占用随它删除）不投。
- 发起方被停止：停止事务清掉占用，并给做这些工作的 thread 各发一条定向命令 StopThread（逐层由它们自己的停止继续）。
- 运行中检查（ThreadChecks）：发起方没停在这次调用上时（它在等时检查信件交不到模型），给发起方投检查信件。

委派的状态只有发起方的占用（active / released / cleared）一处；thread 是哪个会话由关联信件推出，不另存。
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agent_runtime.api import Attachment
from muliao.agents.api import AgentInfo, agent_in
from muliao.platform.db import Database, utcnow
from muliao.platform.tables import Task, TaskStatus
from muliao.platform.tasks import Clock, TaskQueue
from muliao.sessions.activity import WorkState, work_states_in
from muliao.sessions.events import Ending
from muliao.sessions.mailbox import latest_runs_among, post_check_in
from muliao.sessions.pending import AgentAnswer, pending_in
from muliao.sessions.tables import HoldStatus, Mail, Session, SessionHold
from muliao.sessions.work import deliver_in

log = logging.getLogger(__name__)


def delegation_holder(owner_id: str, key: str) -> str:
    """委派在发起方会话上的占用者，也是 thread 信件的关联 id。"""
    return f"delegation:{owner_id}:{key}"


def thread_source(child_id: str) -> str:
    """thread 交回的事件的来源，也用来判断它是否已有信件在发起方的信箱里排着。"""
    return f"thread:{child_id}"


def thread_name(child: Session) -> str:
    if child.thread is None:
        raise RuntimeError(f"session {child.id} is a child session without a thread name")
    return child.thread


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
    holders: Sequence[str],
    ending: Ending,
    text: str,
    attachments: Sequence[Attachment],
) -> None:
    """thread 结清了这些委派：在同一个事务里把结局交给各自的发起方（deliver_in，唯一出口）。每个关联只结清一次
    （mail.correlation_settled），所以每项委派只交回一次。"""
    if not holders:
        return
    name = thread_name(child)
    owner_id = child.parent_session_id
    if owner_id is None:
        # 发起方的唯一来源是 thread 的父会话（Threads.dispatch 总在发起方下开 thread）；holder 对这里是不透明的键。
        # 父会话置空说明发起方已被删除，结局没有收件人
        log.info(
            "delegation outcomes of a thread whose asker is gone dropped", extra={"holders": list(holders)}
        )
        return
    agent = await agent_in(s, child.agent_id)
    for holder in holders:
        # 发起方已删除时 GONE；发起方还在却没有这个占用是 bug，deliver_in 报错
        delivered = await deliver_in(
            s,
            queue,
            owner_id,
            holder,
            key=f"{holder}:settled",
            source=thread_source(child.id),
            event_text=event_text(name, agent.name, ending, text),
            answer=AgentAnswer(
                text=_result_text(name, agent.name, ending, text),
                attachments=list(attachments),
                thread=child.id,
            ),
        )
        log.info(
            "delegation handed back",
            extra={
                "holder": holder,
                "session_id": owner_id,
                "thread_session_id": child.id,
                "ending": ending,
                "delivered_as": delivered,
            },
        )


async def threads_of_holds_in(s: AsyncSession, holders: Sequence[str]) -> list[str]:
    """做这些委派的 thread（由以 holder 为关联 id 的信件推出），按出现顺序去重。不是委派的占用（后台任务）没有
    这样的信件。"""
    if not holders:
        return []
    rows = await s.scalars(
        select(Mail.session_id).where(Mail.correlation.in_(list(holders))).order_by(Mail.id)
    )
    return list(dict.fromkeys(rows.all()))


class ThreadChecks:
    """委派的运行中检查（platform.checks.WorkWatcher 的一种工作）。"""

    def __init__(self, db: Database, queue: TaskQueue, clock: Clock = utcnow):
        self._db = db
        self._queue = queue
        self._clock = clock

    async def check(self) -> int:
        posted = 0
        async with self._db.transaction() as s:
            rows = (
                await s.execute(
                    select(SessionHold, Mail.session_id)
                    .join(Mail, Mail.correlation == SessionHold.holder)
                    .where(SessionHold.status == HoldStatus.ACTIVE)
                    .order_by(SessionHold.id)
                )
            ).all()
            due: dict[tuple[str, str], SessionHold] = {}
            # 按发起会话取一次（一个会话可能同时有几个委派）
            pending: dict[str, Collection[str]] = {}
            for hold, child_id in rows:
                if (hold.session_id, child_id) in due:
                    continue
                if hold.call_id is not None:
                    if hold.session_id not in pending:
                        pending[hold.session_id] = (await pending_in(s, hold.session_id)).keys()
                    if hold.call_id in pending[hold.session_id]:
                        # 发起方停在这次调用上：检查信件要等回答一起才交给模型，没有意义
                        continue
                due[(hold.session_id, child_id)] = hold
            child_ids = {child_id for _, child_id in due}
            works = await work_states_in(s, self._queue, child_ids)
            runs = await latest_runs_among(s, child_ids)
            children = {
                x.id: x
                for x in (await s.scalars(select(Session).where(Session.id.in_(list(child_ids))))).all()
            }
            agents: dict[str, AgentInfo] = {}
            for (_, child_id), hold in due.items():
                child = children.get(child_id)
                if child is None:
                    # 不变量断言：信件随会话删除，join 不会给出已删除的 thread
                    raise RuntimeError(f"delegation {hold.holder} has mail in a missing thread {child_id}")
                if child.agent_id not in agents:
                    agents[child.agent_id] = await agent_in(s, child.agent_id)
                text = check_text(
                    child, agents[child.agent_id], works[child_id], runs.get(child_id), self._clock()
                )
                posted += await post_check_in(
                    s, self._queue, hold.session_id, hold.holder, thread_source(child.id), text
                )
        return posted


def check_text(child: Session, agent: AgentInfo, work: WorkState, run: Task | None, now: datetime) -> str:
    """还没结清的委派的检查信件，按 thread 的工作状态（activity.work_states_in）说；run 是它最近一次运行（还没有
    运行时为 None），只用来补充细节：在跑多久了、在等重试，还是还没开始处理。"""
    name = thread_name(child)
    who = f"Thread {name!r} ({agent.name})"
    keep = f"Keep waiting, you will get an event with its answer; if it is no longer needed, call stop_thread({name!r})."
    match work:
        case WorkState.WAITING:
            doing = (
                "is waiting for work it started (background jobs, other agents) or for answers it asked for"
            )
        case WorkState.RUNNING if run is not None and run.status == TaskStatus.RUNNING:
            if run.started_at is None:
                raise RuntimeError(f"running run {run.id} has no start time")
            minutes = int((now - run.started_at).total_seconds() // 60)
            return f"{who} is still working on your task ({minutes} min so far).\n{keep}"
        case WorkState.RUNNING if run is not None and run.status == TaskStatus.PENDING and run.attempts:
            doing = f"is waiting to run again (attempt {run.attempts} so far)"
        case WorkState.RUNNING | WorkState.IDLE:
            # idle 也会出现：委派信件到了，投信或运行结束（含最终失败）时的唤醒没做成（事务失败、重启），要等定期对账
            # （MailReconciler）补唤醒下一次运行
            doing = "has your task queued and has not started on it yet"
    return f"{who} {doing}. {keep}"
