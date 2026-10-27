"""委派：会话交给另一个 agent、要交回结局的工作（ask_agent，delegate_task(report_back)）。整个生命周期在会话领域里，
由两个对称的事务完成（docs/architecture.md 第 8 节）：

- 发出（Threads.dispatch）：一个事务里在发起方登记占用（holder 由工具调用 id 确定性派生，带 call_id 与「用户是否在等
  发起方」），往 thread 投一封以 holder 为关联 id 的信件。
- 交回（hand_back_in，由 thread 结清关联时调用：运行做完、最终失败、被停止、被删除）：同一个事务里经 deliver_in 把
  结局交给发起方并释放占用——发起方还停在那次 ask_agent 调用上时作为调用的结果，否则作为事件；发起方已被停止
  （占用 cleared）不投，已删除（占用随它删除）不投。
- 发起方被停止：停止事务清掉占用，并给做这些工作的 thread 各发一条定向命令 StopThread（逐层由它们自己的停止继续）。
- 运行中检查与超时（ThreadChecks）：并发等待的（report_back）按 heartbeat_seconds 给发起方投检查信件；阻塞等待的
  （ask_agent）到了 timeout 停掉 thread，结局以「超时被停」交回。

委派的进行状态只有发起方的占用（active / released / cleared）一处；发起参数（thread、心跳间隔、超时）在委派记录
（sessions.delegations）。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agent_runtime.api import Attachment
from muliao.agents.api import AgentInfo, agent_in
from muliao.platform.db import Database, utcnow
from muliao.platform.tasks import Clock, TaskQueue, TaskView
from muliao.sessions.activity import WorkState, work_states_in
from muliao.sessions.delegations import DelegationRecord, records_among
from muliao.sessions.directory import among_in
from muliao.sessions.holds import active_holders_in, calls_in
from muliao.sessions.mailbox import post_check_in
from muliao.sessions.pending import AgentAnswer
from muliao.sessions.runs import latest_runs_among
from muliao.sessions.sources import thread_source
from muliao.sessions.tables import Session
from muliao.sessions.work import check_due, deliver_in

if TYPE_CHECKING:
    # halt 用到本模块（停止时找委派的 thread），这里只引用停止的端口类型
    from muliao.sessions.halt import StopSession

log = logging.getLogger(__name__)


class Ending(StrEnum):
    """关联（委派来的工作）的结局。"""

    FINISHED = "finished"
    STOPPED = "stopped"
    FAILED = "failed"
    DELETED = "deleted"


def delegation_holder(owner_id: str, key: str) -> str:
    """委派在发起方会话上的占用者，也是 thread 信件的关联 id。"""
    return f"delegation:{owner_id}:{key}"


def thread_name(child: Session) -> str:
    if child.thread is None:
        raise RuntimeError(f"session {child.id} is a child session without a thread name")
    return child.thread


def _result_text(thread: str, agent: str, ending: Ending, text: str, timed_out_after: float | None) -> str:
    """作为 ask_agent 调用的结果：完成时就是回答本身（发问方问的就是它）。"""
    if ending == Ending.FINISHED:
        return text
    return event_text(thread, agent, ending, text, timed_out_after)


def event_text(
    thread: str, agent: str, ending: Ending, text: str, timed_out_after: float | None = None
) -> str:
    """timed_out_after：这项委派到了发起方给的 timeout（秒）、thread 因此被停掉。"""
    who = f"Thread {thread!r} ({agent})"
    match ending:
        case Ending.FINISHED:
            return f"{who} finished your task. Its answer:\n{text}"
        case Ending.STOPPED if timed_out_after is not None:
            return (
                f"{who} did not finish within your timeout of {timed_out_after:g} seconds and was stopped. "
                f"What it said so far:\n{text or '(nothing)'}"
            )
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
    now = utcnow()
    # 发起方已删除时记录随它删除，结局也没有收件人
    records = await records_among(s, holders) if ending == Ending.STOPPED else {}
    for holder in holders:
        # deadline 与这里的 now 都用真实时钟（delegations.record_in 与 ThreadChecks 在生产里都是 utcnow）
        timed_out_after = records[holder].timed_out_after(now) if holder in records else None
        # 发起方已删除时 GONE；发起方还在却没有这个占用是 bug，deliver_in 报错
        delivered = await deliver_in(
            s,
            queue,
            owner_id,
            holder,
            key=f"{holder}:settled",
            source=thread_source(child.id),
            event_text=event_text(name, agent.name, ending, text, timed_out_after),
            answer=AgentAnswer(
                text=_result_text(name, agent.name, ending, text, timed_out_after),
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


async def threads_of_holds_in(s: AsyncSession, holders: Sequence[str]) -> dict[str, str]:
    """委派占用者 → 做这项委派的 thread（委派的记录，sessions.delegations）。不是委派的占用（后台任务）与 thread 已被
    删除的委派不在结果里。"""
    return {
        holder: record.thread_id
        for holder, record in (await records_among(s, holders)).items()
        if record.thread_id is not None
    }


async def call_threads_in(s: AsyncSession, session_id: str) -> dict[str, str]:
    """这个会话里交出去的挂起调用（ask_agent）→ 做它的 thread。调用还挂着、没有结果时，界面靠它把 thread 显示在调用处
    （有了结果之后由结果上的 TOOL_THREADS 关联）。"""
    calls = await calls_in(s, session_id)
    threads = await threads_of_holds_in(s, list(calls.values()))
    # 不在 threads 里的：thread 已删除
    return {call_id: threads[holder] for call_id, holder in calls.items() if holder in threads}


class ThreadChecks:
    """委派的运行中检查与超时（WorkWatcher 的一种工作，sessions.work），按委派记录的发起参数（sessions.delegations），
    只看发起方占用还 active 的：
    - 并发等待的委派（delegate_task(report_back)，带 check_seconds）：每过一个间隔给发起方投一封检查信件；
    - 阻塞等待的委派（ask_agent，带 deadline）：到期时停掉做它的 thread（同 stop_thread），结局以「超时被停」
      交回（hand_back_in 按 deadline 推出）。"""

    def __init__(self, db: Database, queue: TaskQueue, stop_session: StopSession, clock: Clock = utcnow):
        self._db = db
        self._queue = queue
        self._stop = stop_session
        self._clock = clock
        # 并发委派上一次投检查的时刻（还没投过的以登记时刻算）：只是节拍，重启后从登记时刻重新算，不落库
        self._last_check: dict[str, datetime] = {}
        # 已经因超时停掉 thread 的阻塞委派：停止是异步的（运行退出时才结清），停下来之前的检查不再停一次
        self._timed_out: set[str] = set()

    async def check(self) -> int:
        posted = 0
        now = self._clock()
        expired: dict[str, str] = {}
        async with self._db.transaction() as s:
            active = await active_holders_in(s)
            records = [
                r
                for r in (await records_among(s, list(active))).values()
                if r.check_seconds is not None or r.deadline is not None
            ]
            watched = {r.holder for r in records}
            for gone in set(self._last_check) - watched:
                del self._last_check[gone]
            self._timed_out &= watched
            # (发起会话, thread) -> 到期的委派与它的间隔
            due: dict[tuple[str, str], tuple[DelegationRecord, float]] = {}
            for record in records:
                child_id = record.thread_id
                if child_id is None:
                    # thread 删除时委派已经以「被删除」结清，占用随之释放
                    raise RuntimeError(f"active delegation {record.holder} has a deleted thread")
                if record.deadline is not None:
                    if record.deadline <= now and record.holder not in self._timed_out:
                        expired[record.holder] = child_id
                    continue
                interval = record.check_seconds
                if interval is None:
                    raise RuntimeError(
                        f"delegation {record.holder} has neither a deadline nor a check interval"
                    )
                if not check_due(self._last_check.get(record.holder, record.created_at), interval, now):
                    continue
                self._last_check[record.holder] = now
                # 同一个发起方对同一个 thread 的几项委派同时到期，检查一次
                due.setdefault((record.session_id, child_id), (record, interval))
            child_ids = {child_id for _, child_id in due}
            works = await work_states_in(s, self._queue, child_ids)
            runs = await latest_runs_among(s, self._queue, child_ids)
            children = await among_in(s, child_ids)
            agents: dict[str, AgentInfo] = {}
            for (_, child_id), (record, interval) in due.items():
                child = children.get(child_id)
                if child is None:
                    # 不变量断言：thread 删除时记录上的引用置空，不会给出已删除的 thread
                    raise RuntimeError(f"delegation {record.holder} points to a missing thread {child_id}")
                if child.agent_id not in agents:
                    agents[child.agent_id] = await agent_in(s, child.agent_id)
                text = check_text(
                    child, agents[child.agent_id], works[child_id], runs.get(child_id), now, interval
                )
                posted += await post_check_in(
                    s, self._queue, record.session_id, record.holder, thread_source(child.id), text
                )
        for holder, child_id in expired.items():
            log.info(
                "delegation timed out; stopping its thread",
                extra={"holder": holder, "thread_session_id": child_id},
            )
            # 停止自己开事务（还在运行时在运行退出后结清）。每项是独立的副作用：一项停不下来（如数据库暂时失败）
            # 记 error、不记下，下一拍再停，不影响别的到期委派，也不让检查循环退出
            try:
                await self._stop(child_id)
            except Exception:
                log.exception(
                    "stopping a timed-out delegation's thread failed; retrying on the next check",
                    extra={"holder": holder, "thread_session_id": child_id},
                )
                continue
            self._timed_out.add(holder)
        return posted


def check_text(
    child: Session,
    agent: AgentInfo,
    work: WorkState,
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
    match work:
        case WorkState.WAITING:
            doing = (
                "is waiting for work it started (background jobs, other agents) or for answers it asked for"
            )
        case WorkState.RUNNING if run is not None and run.running:
            if run.started_at is None:
                raise RuntimeError(f"running run {run.id} has no start time")
            minutes, seconds = divmod(int((now - run.started_at).total_seconds()), 60)
            return f"{who} is still working on your task ({minutes}m{seconds:02d}s so far).\n{keep}"
        case WorkState.RUNNING if run is not None and run.pending and run.attempts:
            doing = f"is waiting to run again (attempt {run.attempts} so far)"
        case WorkState.RUNNING | WorkState.IDLE:
            # idle 也会出现：委派信件到了，投信或运行结束（含最终失败）时的唤醒没做成（事务失败、重启），要等定期对账
            # （MailReconciler）补唤醒下一次运行
            doing = "has your task queued and has not started on it yet"
    return f"{who} {doing}. {keep}"
