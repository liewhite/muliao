"""别的领域看到的会话（读模型）：ORM 行不出会话领域。"""

from collections.abc import Collection
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.platform.tooling import Origin
from muliao.sessions.kinds import SessionKind
from muliao.sessions.tables import Session


@dataclass(frozen=True)
class SessionInfo:
    """别的领域看到的会话（读模型）：ORM 行不出会话领域。"""

    id: str
    agent_id: str
    title: str
    # 委派开出的 thread：它在父会话里的名字
    thread: str | None
    # 会话的发起方：来源与它在来源里的身份（触发器 id、issue id……；委派与用户为 None）
    origin: Origin
    origin_ref: str | None


async def session_in(s: AsyncSession, session_id: str) -> SessionInfo | None:
    """会话（已删除为 None）。会话只会从存在变为删除，在调用方的事务里读没有先查后写的竞态。"""
    session = await s.get(Session, session_id)
    if session is None:
        return None
    return _info(session)


async def keyed_sessions_among(
    s: AsyncSession, origin: Origin, origin_refs: Collection[str]
) -> dict[str, dict[str, SessionInfo]]:
    """按发送方定位的会话（inbound.SessionKey）：每个发送方身份 -> {agent id -> 它的会话}，同一规则（最新的顶层会话），
    一次查询。用来展示、判断一批 issue 各自的会话（发送方不另存会话 id）。"""
    found: dict[str, dict[str, SessionInfo]] = {ref: {} for ref in origin_refs}
    query = (
        select(Session)
        .where(
            Session.origin == origin,
            Session.origin_ref.in_(origin_refs),
            Session.kind == SessionKind.CONVERSATION,
        )
        .order_by(Session.created_at, Session.id)
    )
    for session in (await s.scalars(query)).all():
        found[session.origin_ref or ""][session.agent_id] = _info(session)
    return found


def _info(session: Session) -> SessionInfo:
    return SessionInfo(
        id=session.id,
        agent_id=session.agent_id,
        title=session.title,
        thread=session.thread,
        origin=session.origin,
        origin_ref=session.origin_ref,
    )
