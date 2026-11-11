"""别的领域看到的会话（读模型）：ORM 行不出会话领域。"""

from collections.abc import Collection
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.sessions.directory import keyed_rows_among
from muliao.sessions.origins import Sender, SessionOrigin, origin_of
from muliao.sessions.tables import Session


@dataclass(frozen=True)
class SessionInfo:
    """别的领域看到的会话（读模型）：ORM 行不出会话领域。"""

    id: str
    agent_id: str
    title: str
    # 委派开出的 thread：它在父会话里的名字
    thread: str | None
    # 会话的来源与发起方
    origin: SessionOrigin


async def session_in(s: AsyncSession, session_id: str) -> SessionInfo | None:
    """会话（已删除为 None）。会话只会从存在变为删除，在调用方的事务里读没有先查后写的竞态。"""
    session = await s.get(Session, session_id)
    if session is None:
        return None
    return info_of(session)


async def keyed_session_in(s: AsyncSession, sender: Sender, agent_id: str) -> SessionInfo | None:
    """这个发送方给这个 agent 开的会话（规则见 directory.keyed_rows_among）；还没有（或已删除）为 None。"""
    session = (await keyed_rows_among(s, [sender], agent_id)).get((sender, agent_id))
    return None if session is None else info_of(session)


async def keyed_sessions_among(
    s: AsyncSession, senders: Collection[Sender]
) -> dict[Sender, dict[str, SessionInfo]]:
    """按发送方定位的会话（规则见 directory.keyed_rows_among）：每个发送方 -> {agent id -> 它的会话}。用来展示、判断一批
    issue 各自的会话（发送方不另存会话 id）。"""
    found: dict[Sender, dict[str, SessionInfo]] = {x: {} for x in senders}
    for (sender, agent_id), session in (await keyed_rows_among(s, senders)).items():
        found[sender][agent_id] = info_of(session)
    return found


def info_of(session: Session) -> SessionInfo:
    return SessionInfo(
        id=session.id,
        agent_id=session.agent_id,
        title=session.title,
        thread=session.thread,
        origin=origin_of(session),
    )
