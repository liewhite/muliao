"""会话的归属：哪些 thread 归父会话所有、随它删除（KindPolicy.owned_by_parent）。删除与删除前的检查共用。"""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.sessions.kinds import OWNED_BY_PARENT
from muliao.sessions.tables import Session


async def owned_threads_in(s: AsyncSession, roots: Sequence[Session]) -> dict[str, Session]:
    """这些会话连同（逐层）归它们所有的 thread（KindPolicy.owned_by_parent）：删除时一起删除。"""
    doomed = {x.id: x for x in roots}
    frontier = list(doomed)
    while frontier:
        children = (
            await s.scalars(
                select(Session).where(
                    Session.parent_session_id.in_(frontier), Session.kind.in_(OWNED_BY_PARENT)
                )
            )
        ).all()
        frontier = [c.id for c in children if c.id not in doomed]
        doomed |= {c.id: c for c in children}
    return doomed
