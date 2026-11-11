"""占用：别的领域（或会话自己约的到点唤醒、发出的委派）替一个会话在外面做的一项异步工作。外部工作按不透明的
占用者认（后台任务 job:<id>、到点唤醒 wake:<会话>:<键>），委派的占用只引用委派行（thread_call_id，委派的主体是
sessions.thread_calls）。会话只知道「有人替我在外面做事」，不知道是什么事。设计见 docs/architecture.md 第 7 节。

- 有 active 占用：会话的工作还没做完，交给它的委派不结清（sessions.finish、sessions.settlement）。委派的占用还 active 就是
  这项委派还没结清（唯一的判断）。
- 带 call_id 的占用：这次挂起调用（ask_agent）已经交出去了。不论状态都算，是永久事实（sessions.pending）。
- 结局交回时释放（和一封信件同一事务）；会话被停止、或自己放弃（abandon）时清掉（之后的结局不再投）。行永久保留。
"""

from __future__ import annotations

from collections.abc import Collection

from sqlalchemy import Subquery, select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.platform.db import utcnow
from muliao.sessions.tables import HoldStatus, SessionHold


async def hold_in(s: AsyncSession, session_id: str, holder: str, *, awaited: bool) -> SessionHold:
    """登记一项外部工作的占用；已经登记过（重放）就返回它。awaited：用户是否在等这项工作的结局（交回的信件随它推送）。"""
    existing = await get_in(s, session_id, holder)
    if existing is not None:
        if existing.awaited != awaited:
            raise RuntimeError(
                f"hold {holder} of session {session_id} was registered with awaited={existing.awaited}, not {awaited}"
            )
        return existing
    return await _add_in(s, SessionHold(session_id=session_id, holder=holder, awaited=awaited))


async def hold_delegation_in(
    s: AsyncSession, session_id: str, thread_call_id: int, *, call_id: str | None, awaited: bool
) -> SessionHold:
    """发起方登记一项委派的占用，引用委派行（发出的幂等由委派行的唯一键保证，sessions.delegation）。call_id：结局作为
    这次挂起调用的结果交回（ask_agent）；并发等待的为 None。"""
    return await _add_in(
        s, SessionHold(session_id=session_id, thread_call_id=thread_call_id, call_id=call_id, awaited=awaited)
    )


async def _add_in(s: AsyncSession, hold: SessionHold) -> SessionHold:
    hold.created_at = utcnow()
    s.add(hold)
    await s.flush()
    return hold


async def get_in(s: AsyncSession, session_id: str, holder: str) -> SessionHold | None:
    """外部工作在会话上的占用。"""
    return await s.scalar(
        select(SessionHold).where(SessionHold.session_id == session_id, SessionHold.holder == holder)
    )


async def registered_in(s: AsyncSession, session_id: str, holder: str) -> bool:
    """这项工作在会话上登记过占用（行永久保留，随会话删除）。"""
    return await get_in(s, session_id, holder) is not None


async def active_delegations_in(
    s: AsyncSession, thread_call_ids: Collection[int] | None = None
) -> list[SessionHold]:
    """还没结清的委派的占用（active、引用委派行），按登记先后；给了 thread_call_ids 只取这些委派的（一次查询）。"""
    query = select(SessionHold).where(
        SessionHold.status == HoldStatus.ACTIVE, SessionHold.thread_call_id.is_not(None)
    )
    if thread_call_ids is not None:
        if not thread_call_ids:
            return []
        query = query.where(SessionHold.thread_call_id.in_(list(thread_call_ids)))
    return list((await s.scalars(query.order_by(SessionHold.id))).all())


def release(hold: SessionHold) -> HoldStatus:
    """把占用记为已释放（随调用方的事务提交），返回释放前的状态。已经被停止清掉（cleared）的不变：调用方不再投信。"""
    before = hold.status
    if before == HoldStatus.ACTIVE:
        hold.status, hold.ended_at = HoldStatus.RELEASED, utcnow()
    return before


def abandon(hold: SessionHold) -> None:
    """会话自己放弃一项还在进行的工作（改期、取消到点唤醒）：记为 cleared，同停止，之后的结局不再投。"""
    if hold.status != HoldStatus.ACTIVE:
        raise RuntimeError(f"hold {hold.id} of session {hold.session_id} is {hold.status}, not active")
    hold.status, hold.ended_at = HoldStatus.CLEARED, utcnow()


async def clear_in(s: AsyncSession, session_id: str) -> list[SessionHold]:
    """会话被停止：全部 active 占用记为 cleared，返回它们。"""
    holds = list(
        (
            await s.scalars(
                select(SessionHold).where(
                    SessionHold.session_id == session_id, SessionHold.status == HoldStatus.ACTIVE
                )
            )
        ).all()
    )
    for hold in holds:
        abandon(hold)
    return holds


async def active_holders_of_in(s: AsyncSession, session_id: str) -> list[str]:
    """这个会话的外部工作的 active 占用者。"""
    rows = await s.scalars(
        select(SessionHold.holder).where(
            SessionHold.session_id == session_id,
            SessionHold.status == HoldStatus.ACTIVE,
            SessionHold.holder.is_not(None),
        )
    )
    return [holder for holder in rows.all() if holder is not None]


def active_holds() -> Subquery:
    """全部 active 占用（列 holder、created_at、id；id 即登记先后），给别的模块在一条查询里按外部工作的占用者连表（到点
    唤醒按它找还在进行的唤醒），不先把全部占用者取出来。"""
    return (
        select(SessionHold.holder, SessionHold.created_at, SessionHold.id)
        .where(SessionHold.status == HoldStatus.ACTIVE)
        .subquery("active_holds")
    )


async def has_active_in(s: AsyncSession, session_id: str) -> bool:
    return (
        await s.scalar(
            select(SessionHold.id)
            .where(SessionHold.session_id == session_id, SessionHold.status == HoldStatus.ACTIVE)
            .limit(1)
        )
    ) is not None


async def with_active_among(s: AsyncSession, session_ids: Collection[str]) -> set[str]:
    """其中有 active 占用的会话（一次查询）。"""
    rows = await s.scalars(
        select(SessionHold.session_id)
        .where(SessionHold.session_id.in_(list(session_ids)), SessionHold.status == HoldStatus.ACTIVE)
        .distinct()
    )
    return set(rows.all())


async def active_among(s: AsyncSession, session_ids: Collection[str]) -> dict[str, list[SessionHold]]:
    """其中每个会话的 active 占用，按登记先后（一次查询）；没有的会话不在结果里。"""
    rows = await s.scalars(
        select(SessionHold)
        .where(SessionHold.session_id.in_(list(session_ids)), SessionHold.status == HoldStatus.ACTIVE)
        .order_by(SessionHold.id)
    )
    held: dict[str, list[SessionHold]] = {}
    for hold in rows.all():
        held.setdefault(hold.session_id, []).append(hold)
    return held


async def called_in(s: AsyncSession, session_id: str) -> set[str]:
    """这个会话里已经交出去的挂起调用 id（带 call_id 的占用，不论状态）。"""
    rows = await s.scalars(
        select(SessionHold.call_id).where(
            SessionHold.session_id == session_id, SessionHold.call_id.is_not(None)
        )
    )
    return {call_id for call_id in rows.all() if call_id is not None}


async def called_among(s: AsyncSession, session_ids: Collection[str]) -> dict[str, set[str]]:
    """called_in 的批量版本（一次查询），每个会话都在结果里。"""
    found: dict[str, set[str]] = {x: set() for x in session_ids}
    rows = await s.execute(
        select(SessionHold.session_id, SessionHold.call_id).where(
            SessionHold.session_id.in_(list(session_ids)), SessionHold.call_id.is_not(None)
        )
    )
    for session_id, call_id in rows.all():
        if call_id is not None:
            found[session_id].add(call_id)
    return found
