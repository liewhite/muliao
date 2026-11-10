"""调用开出的 thread（thread_calls 表的唯一读写处）：哪次工具调用开出或续写了哪个 thread。开出/续写 thread 的事务里
记下（sessions.threads），会话接口按调用 id 把 thread 挂在调用条目上（sessions.service），不看调用的结果。

要交回结局的调用（ask_agent、delegate_task(report_back)）就是一项委派：同一行再记它在发起方的占用者与心跳间隔，是
委派的记录。委派还在不在进行看发起方的占用（sessions.holds），结清没有看 thread 的信件（sessions.settlement）；
流程在 sessions.delegation / sessions.threads。"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.sessions.holds import active_holds
from muliao.sessions.tables import ThreadCall


@dataclass(frozen=True)
class DelegationRecord:
    holder: str
    # 发起会话
    parent_id: str
    # 做它的 thread（thread 删除时记录随之删除）
    thread_id: str
    # 并发等待（report_back）的心跳间隔；阻塞等待（ask_agent）为 None
    check_seconds: float | None


@dataclass(frozen=True)
class ActiveDelegation:
    record: DelegationRecord
    # 发起方登记占用的时刻（运行中检查从它起算）
    since: datetime
    # 运行中检查的间隔（秒）
    interval: float


def _record(row: ThreadCall) -> DelegationRecord:
    if row.holder is None:
        raise RuntimeError(f"call {row.call_id} of session {row.parent_session_id} is not a delegation")
    return DelegationRecord(
        holder=row.holder,
        parent_id=row.parent_session_id,
        thread_id=row.thread_session_id,
        check_seconds=row.check_seconds,
    )


async def record_call_in(
    s: AsyncSession,
    parent_id: str,
    call_id: str,
    thread_id: str,
    *,
    holder: str | None = None,
    check_seconds: float | None = None,
) -> None:
    """记下这次调用开出或续写了这个 thread；holder 非空时它是一项委派（Threads.dispatch），带上心跳间隔。续跑重新
    执行同一次调用时已经记过，不再记；同一次调用记成别的样子是 bug。"""
    existing = await s.scalar(
        select(ThreadCall).where(ThreadCall.parent_session_id == parent_id, ThreadCall.call_id == call_id)
    )
    if existing is not None:
        if (existing.thread_session_id, existing.holder, existing.check_seconds) != (
            thread_id,
            holder,
            check_seconds,
        ):
            raise RuntimeError(
                f"call {call_id} of session {parent_id} was recorded as ({existing.thread_session_id}, "
                f"{existing.holder}, {existing.check_seconds}), not ({thread_id}, {holder}, {check_seconds})"
            )
        return
    s.add(
        ThreadCall(
            parent_session_id=parent_id,
            call_id=call_id,
            thread_session_id=thread_id,
            holder=holder,
            check_seconds=check_seconds,
        )
    )
    await s.flush()


async def call_threads_in(s: AsyncSession, parent_id: str) -> dict[str, str]:
    """这个会话里每次开出或续写 thread 的调用 → 那个 thread（已删除的 thread 随之删除，不在结果里）。"""
    rows = await s.execute(
        select(ThreadCall.call_id, ThreadCall.thread_session_id).where(
            ThreadCall.parent_session_id == parent_id
        )
    )
    return {call_id: thread_id for call_id, thread_id in rows.all()}


async def delegations_among(s: AsyncSession, holders: Collection[str]) -> dict[str, DelegationRecord]:
    """这些占用者里是委派的那些的记录（后台任务等别的占用、thread 已删除的委派不在结果里），一次查询。"""
    if not holders:
        return {}
    rows = await s.scalars(select(ThreadCall).where(ThreadCall.holder.in_(list(holders))))
    return {(r := _record(row)).holder: r for row in rows.all()}


async def active_reporting_in(s: AsyncSession) -> list[ActiveDelegation]:
    """发起方占用还 active 的并发等待委派（带心跳间隔的），按登记先后（委派的运行中检查用），一次查询。"""
    active = active_holds()
    rows = await s.execute(
        select(ThreadCall, active.c.created_at, ThreadCall.check_seconds)
        .join(active, active.c.holder == ThreadCall.holder)
        .where(ThreadCall.check_seconds.is_not(None))
        .order_by(active.c.id)
    )
    return [ActiveDelegation(_record(row), since, interval) for row, since, interval in rows.all()]
