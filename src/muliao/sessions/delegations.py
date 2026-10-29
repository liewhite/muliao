"""委派的记录（委派子域，delegations 表的唯一读写处）：每项要交回结局的委派的发起参数——做它的 thread、并发等待的
心跳间隔。委派还在不在进行看发起方的占用（sessions.holds），结清没有看 thread 的信件
（sessions.settlement）；流程在 sessions.delegation / sessions.threads。"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.platform.db import utcnow
from muliao.sessions.tables import Delegation


@dataclass(frozen=True)
class DelegationRecord:
    holder: str
    # 发起会话
    parent_id: str
    # 做它的 thread；已删除为 None
    thread_id: str | None
    # 并发等待（report_back）的心跳间隔；阻塞等待（ask_agent）为 None
    check_seconds: float | None
    created_at: datetime


def _record(row: Delegation) -> DelegationRecord:
    return DelegationRecord(
        holder=row.holder,
        parent_id=row.parent_session_id,
        thread_id=row.thread_session_id,
        check_seconds=row.check_seconds,
        created_at=row.created_at,
    )


async def record_in(
    s: AsyncSession,
    *,
    holder: str,
    parent_id: str,
    thread_id: str,
    check_seconds: float | None,
) -> DelegationRecord:
    """记下一项委派（发出它的事务里，Threads.dispatch）。"""
    row = Delegation(
        holder=holder,
        parent_session_id=parent_id,
        thread_session_id=thread_id,
        check_seconds=check_seconds,
        created_at=utcnow(),
    )
    s.add(row)
    await s.flush()
    return _record(row)


async def records_among(s: AsyncSession, holders: Collection[str]) -> dict[str, DelegationRecord]:
    """这些占用者里是委派的那些的记录（后台任务等别的占用不在结果里），一次查询。"""
    if not holders:
        return {}
    rows = await s.scalars(
        select(Delegation).where(Delegation.holder.in_(list(holders))).order_by(Delegation.id)
    )
    return {row.holder: _record(row) for row in rows.all()}
