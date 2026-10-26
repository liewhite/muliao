"""占用：别的领域替一个会话在外面做的一项异步工作（后台任务 job:<id>、委派 delegation:<会话>:<键>）。会话只知道
「有人替我在外面做事」，不知道是什么事。设计见 docs/architecture.md 第 7 节。

- 有 active 占用：会话的工作还没做完，交给它的委派不结清（sessions.finish、sessions.settlement）。
- 带 call_id 的占用：这次挂起调用（ask_agent）已经交出去了。不论状态都算，是永久事实（sessions.pending）。
- 结局交回时释放（和一封信件同一事务）；会话被停止时清掉（之后的结局不再投）。行永久保留。
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.platform.db import utcnow
from muliao.sessions.tables import HoldStatus, SessionHold


async def hold_in(
    s: AsyncSession,
    session_id: str,
    holder: str,
    *,
    awaited: bool,
    call_id: str | None = None,
    check_seconds: float | None = None,
    timeout_seconds: float | None = None,
) -> SessionHold:
    """登记一个占用；已经登记过（重放）就返回它。awaited：用户是否在等这项工作的结局（交回的信件随它推送）。
    check_seconds：委派的运行中检查间隔；timeout_seconds：从登记起算的超时，记为 deadline（见 SessionHold）。"""
    existing = await get_in(s, session_id, holder)
    if existing is not None:
        if (existing.call_id, existing.awaited) != (call_id, awaited):
            raise RuntimeError(
                f"hold {holder} of session {session_id} was registered for call {existing.call_id} "
                f"(awaited={existing.awaited}), not {call_id} (awaited={awaited})"
            )
        return existing
    now = utcnow()
    hold = SessionHold(
        session_id=session_id,
        holder=holder,
        call_id=call_id,
        awaited=awaited,
        check_seconds=check_seconds,
        deadline=None if timeout_seconds is None else now + timedelta(seconds=timeout_seconds),
        created_at=now,
    )
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


def release(hold: SessionHold) -> HoldStatus:
    """把占用记为已释放（随调用方的事务提交），返回释放前的状态。已经被停止清掉（cleared）的不变：调用方不再投信。"""
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


async def with_active_among(s: AsyncSession, session_ids: Collection[str]) -> set[str]:
    """其中有 active 占用的会话（一次查询）。"""
    rows = await s.scalars(
        select(SessionHold.session_id)
        .where(SessionHold.session_id.in_(list(session_ids)), SessionHold.status == HoldStatus.ACTIVE)
        .distinct()
    )
    return set(rows.all())


async def calls_in(s: AsyncSession, session_id: str) -> dict[str, str]:
    """这个会话里已经交出去的挂起调用（带 call_id 的占用，不论状态）：调用 id → 占用者。"""
    rows = await s.execute(
        select(SessionHold.call_id, SessionHold.holder).where(
            SessionHold.session_id == session_id, SessionHold.call_id.is_not(None)
        )
    )
    return {call_id: holder for call_id, holder in rows.all() if call_id is not None}


async def called_in(s: AsyncSession, session_id: str) -> set[str]:
    """这个会话里已经交出去的挂起调用 id（calls_in 的键）。"""
    return set(await calls_in(s, session_id))
