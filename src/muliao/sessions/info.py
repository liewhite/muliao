"""别的领域看到的会话（读模型）：ORM 行不出会话领域。"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.sessions.tables import Session


@dataclass(frozen=True)
class SessionInfo:
    """别的领域看到的会话（读模型）：ORM 行不出会话领域。"""

    id: str
    agent_id: str
    title: str
    # 委派开出的 thread：它在父会话里的名字
    thread: str | None


async def session_in(s: AsyncSession, session_id: str) -> SessionInfo | None:
    """会话（已删除为 None）。会话只会从存在变为删除，在调用方的事务里读没有先查后写的竞态。"""
    session = await s.get(Session, session_id)
    if session is None:
        return None
    return SessionInfo(id=session.id, agent_id=session.agent_id, title=session.title, thread=session.thread)
