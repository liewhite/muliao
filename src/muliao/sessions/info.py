"""别的领域看到的会话（读模型）：ORM 行不出会话领域。"""

from collections.abc import Collection
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.platform.tooling import Origin
from muliao.sessions.kinds import TOP_LEVEL
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


async def keyed_rows_among(
    s: AsyncSession, origin: Origin, origin_refs: Collection[str], agent_id: str | None = None
) -> dict[tuple[str, str], Session]:
    """按发送方定位会话（inbound.SessionKey）的唯一规则：(发送方身份, agent id) -> 这个发送方给这个 agent 开过的最新
    一个顶层会话，一次查询。给出 agent_id 时只看这个 agent。会话领域内部用（ORM 行）。"""
    query = (
        select(Session)
        .where(
            Session.origin == origin,
            Session.origin_ref.in_(list(origin_refs)),
            Session.kind.in_(TOP_LEVEL),
        )
        .order_by(Session.created_at, Session.id)
    )
    if agent_id is not None:
        query = query.where(Session.agent_id == agent_id)
    found: dict[tuple[str, str], Session] = {}
    for session in (await s.scalars(query)).all():
        if session.origin_ref is None:
            raise RuntimeError(f"keyed lookup matched session {session.id} without an origin_ref")
        # 按创建先后排序，后面的覆盖前面的：留下最新的
        found[(session.origin_ref, session.agent_id)] = session
    return found


async def keyed_sessions_among(
    s: AsyncSession, origin: Origin, origin_refs: Collection[str]
) -> dict[str, dict[str, SessionInfo]]:
    """按发送方定位的会话（规则见 keyed_rows_among）：每个发送方身份 -> {agent id -> 它的会话}。用来展示、判断一批
    issue 各自的会话（发送方不另存会话 id）。"""
    found: dict[str, dict[str, SessionInfo]] = {ref: {} for ref in origin_refs}
    for (ref, agent_id), session in (await keyed_rows_among(s, origin, origin_refs)).items():
        found[ref][agent_id] = _info(session)
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
