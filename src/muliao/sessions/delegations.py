"""委派的记录（委派子域，delegations 表的唯一读写处）：每项要交回结局的委派的发起参数——做它的 thread、并发等待的
心跳间隔、阻塞等待的超时。委派还在不在进行看发起方的占用（sessions.holds），结清没有看 thread 的信件
（sessions.settlement）；流程在 sessions.delegation / sessions.threads。"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta

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
    check_seconds: float | None
    deadline: datetime | None
    created_at: datetime

    def timed_out_after(self, now: datetime) -> float | None:
        """到了 deadline（阻塞委派的 timeout）就返回那个 timeout（秒），否则 None。由 deadline 推出，不另存停止原因：
        到期之后才被停止的，不论是谁停的，都已经超时。"""
        if self.deadline is None or self.deadline > now:
            return None
        return (self.deadline - self.created_at).total_seconds()


def _record(row: Delegation) -> DelegationRecord:
    return DelegationRecord(
        holder=row.holder,
        parent_id=row.parent_session_id,
        thread_id=row.thread_session_id,
        check_seconds=row.check_seconds,
        deadline=row.deadline,
        created_at=row.created_at,
    )


async def record_in(
    s: AsyncSession,
    *,
    holder: str,
    parent_id: str,
    thread_id: str,
    check_seconds: float | None,
    timeout_seconds: float | None,
) -> DelegationRecord:
    """记下一项委派（发出它的事务里，Threads.dispatch）。timeout_seconds 从现在起算，记为 deadline。"""
    if check_seconds is not None and timeout_seconds is not None:
        raise ValueError(
            f"delegation {holder} is either concurrent (check_seconds) or blocking (timeout), not both"
        )
    now = utcnow()
    row = Delegation(
        holder=holder,
        parent_session_id=parent_id,
        thread_session_id=thread_id,
        check_seconds=check_seconds,
        deadline=None if timeout_seconds is None else now + timedelta(seconds=timeout_seconds),
        created_at=now,
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
