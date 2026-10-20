"""占用：别的领域替一个会话在外面做的一项异步工作（后台任务 job:<id>、委派 delegation:<会话>:<键>）。会话只知道
「有人替我在外面做事」，不知道是什么事。设计见 docs/domain-design.md 4.1。

- 有 active 占用：会话的工作还没做完，交给它的委派不结清（sessions.finish、sessions.settlement）。
- 带 call_id 的占用：这次挂起调用（ask_agent）已经交出去了。不论状态都算，是永久事实（sessions.pending）。
- 结局交回时释放（和一封信件同一事务）；会话被停止时清掉（之后的结局不再投）。行永久保留。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.platform.db import utcnow
from muliao.sessions.tables import HoldStatus, SessionHold


async def hold_in(s: AsyncSession, session_id: str, holder: str, call_id: str | None = None) -> SessionHold:
    """登记一个占用；已经登记过（重放）就返回它。"""
    existing = await get_in(s, session_id, holder)
    if existing is not None:
        if existing.call_id != call_id:
            raise RuntimeError(
                f"hold {holder} of session {session_id} was registered for call {existing.call_id}, not {call_id}"
            )
        return existing
    hold = SessionHold(session_id=session_id, holder=holder, call_id=call_id)
    s.add(hold)
    await s.flush()
    return hold


async def get_in(s: AsyncSession, session_id: str, holder: str) -> SessionHold | None:
    return await s.scalar(
        select(SessionHold).where(SessionHold.session_id == session_id, SessionHold.holder == holder)
    )


async def registered_in(s: AsyncSession, session_id: str, holder: str) -> bool:
    """这项工作在会话上登记过占用（行永久保留，随会话删除）。"""
    return await get_in(s, session_id, holder) is not None


async def release_in(s: AsyncSession, session_id: str, holder: str) -> HoldStatus:
    """把占用记为已释放，返回释放前的状态。已经被停止清掉（cleared）的不变：调用方不再投信。会话存在而占用从未登记
    过是 bug，直接报错。"""
    hold = await get_in(s, session_id, holder)
    if hold is None:
        raise RuntimeError(f"session {session_id} has no hold {holder}")
    before = hold.status
    if before == HoldStatus.ACTIVE:
        hold.status, hold.ended_at = HoldStatus.RELEASED, utcnow()
    return before


async def clear_in(s: AsyncSession, session_id: str) -> list[str]:
    """会话被停止：全部 active 占用记为 cleared，返回它们的占用者。"""
    holds = list(
        (
            await s.scalars(
                select(SessionHold).where(
                    SessionHold.session_id == session_id, SessionHold.status == HoldStatus.ACTIVE
                )
            )
        ).all()
    )
    now = utcnow()
    for hold in holds:
        hold.status, hold.ended_at = HoldStatus.CLEARED, now
    return [h.holder for h in holds]


async def has_active_in(s: AsyncSession, session_id: str) -> bool:
    return (
        await s.scalar(
            select(SessionHold.id)
            .where(SessionHold.session_id == session_id, SessionHold.status == HoldStatus.ACTIVE)
            .limit(1)
        )
    ) is not None


async def called_in(s: AsyncSession, session_id: str) -> set[str]:
    """这个会话里已经交出去的挂起调用（带 call_id 的占用，不论状态）。"""
    rows = await s.scalars(
        select(SessionHold.call_id).where(
            SessionHold.session_id == session_id, SessionHold.call_id.is_not(None)
        )
    )
    return {c for c in rows.all() if c is not None}
