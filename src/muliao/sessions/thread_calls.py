"""调用开出的 thread（thread_calls 表的唯一读写处）：哪次工具调用开出或续写了哪个 thread。开出/续写 thread 的事务里
记下（sessions.threads），会话接口按调用 id 把 thread 挂在调用条目上（sessions.service），不看调用的结果。

要交回结局的调用（ask_agent、delegate_task(report_back)）就是一项委派，这一行是委派的唯一主体（docs/architecture.md
2.4 第 4 行）：发起方的占用（sessions.holds）与 thread 的信件（mail.delegation_id）只引用它的 id，行上再记心跳间隔。
委派结清没有只看引用它的占用是否还 active；流程在 sessions.delegation / sessions.settlement。"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.sessions.holds import active_delegations_in
from muliao.sessions.tables import SessionHold, ThreadCall


@dataclass(frozen=True)
class CallRecord:
    """thread_calls 的一行：一次开出或续写 thread 的调用，发起方有引用它的占用时就是一项委派。"""

    id: int
    # 发起会话
    parent_id: str
    # 做它的 thread（thread 删除时行随之删除）
    thread_id: str
    # 并发等待（report_back）的心跳间隔；阻塞等待（ask_agent）为 None
    check_seconds: float | None


@dataclass(frozen=True)
class Owed:
    """还没结清的委派与发起方引用它的 active 占用（结局经这个占用交回，sessions.work.deliver_in）。"""

    delegation: CallRecord
    hold: SessionHold


def _record(row: ThreadCall) -> CallRecord:
    return CallRecord(
        id=row.id,
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
    check_seconds: float | None = None,
) -> CallRecord:
    """记下这次调用开出或续写了这个 thread（委派的还带心跳间隔），返回这一行。同一次调用只记一次：续跑重新执行
    时已经记过（Threads.send 照常重放），记成别的样子是 bug。"""
    existing = await call_in(s, parent_id, call_id)
    if existing is not None:
        if (existing.thread_id, existing.check_seconds) != (thread_id, check_seconds):
            raise RuntimeError(
                f"call {call_id} of session {parent_id} was recorded as ({existing.thread_id}, "
                f"{existing.check_seconds}), not ({thread_id}, {check_seconds})"
            )
        return existing
    row = ThreadCall(
        parent_session_id=parent_id, call_id=call_id, thread_session_id=thread_id, check_seconds=check_seconds
    )
    s.add(row)
    await s.flush()
    return _record(row)


async def call_in(s: AsyncSession, parent_id: str, call_id: str) -> CallRecord | None:
    """这次调用记下的那一行（还没记过、或 thread 已删除时为 None）。"""
    row = await s.scalar(
        select(ThreadCall).where(ThreadCall.parent_session_id == parent_id, ThreadCall.call_id == call_id)
    )
    return None if row is None else _record(row)


async def call_threads_in(s: AsyncSession, parent_id: str) -> dict[str, str]:
    """这个会话里每次开出或续写 thread 的调用 → 那个 thread（已删除的 thread 随之删除，不在结果里）。"""
    rows = await s.execute(
        select(ThreadCall.call_id, ThreadCall.thread_session_id).where(
            ThreadCall.parent_session_id == parent_id
        )
    )
    return {call_id: thread_id for call_id, thread_id in rows.all()}


async def delegations_among(s: AsyncSession, ids: Collection[int]) -> dict[int, CallRecord]:
    """这些委派行（thread 已删除的不在结果里），一次查询。"""
    if not ids:
        return {}
    rows = await s.scalars(select(ThreadCall).where(ThreadCall.id.in_(list(ids))))
    return {row.id: _record(row) for row in rows.all()}


async def owed_in(s: AsyncSession, ids: Collection[int] | None = None) -> list[Owed]:
    """还没结清的委派（发起方引用它的占用还 active），按占用登记先后；给了 ids 只看这些委派。一次查占用、一次查行。"""
    holds = await active_delegations_in(s, ids)
    rows = await delegations_among(s, [h.thread_call_id for h in holds if h.thread_call_id is not None])
    owed = []
    for hold in holds:
        if hold.thread_call_id not in rows:
            # 外键置空只发生在 thread 删除时，那之前委派已经结清（sessions.deletion）
            raise RuntimeError(f"active hold {hold.id} points to a missing delegation {hold.thread_call_id}")
        owed.append(Owed(rows[hold.thread_call_id], hold))
    return owed
