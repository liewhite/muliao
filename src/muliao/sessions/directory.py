"""会话目录（会话子域）：新建一行、按属性查会话（按发送方定位、按 agent、thread、归属、列表）都只在这里；别的模块按 id
取会话（`s.get`）或调这里的函数，不另写查询（tests/test_boundaries.py 核对）。按 id 取到的行可在领域内改（续写时间
在 inbound / finish），删除在 deletion。
会话的规则（种类、来源、续写）在 kinds / origins / inbound，这里只是查询与写入。"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.sessions.history import sessions_mentioning
from muliao.sessions.kinds import TOP_LEVEL, SessionKind
from muliao.sessions.origins import Origin
from muliao.sessions.tables import Session


async def add_in(
    s: AsyncSession,
    *,
    session_id: str,
    agent_id: str,
    title: str,
    origin: Origin,
    origin_ref: str | None,
    parent_session_id: str | None,
    thread: str | None,
    kind: SessionKind,
) -> Session:
    """写入一个新会话（规则由唯一的入口 inbound.create_session 检查）。"""
    session = Session(
        id=session_id,
        agent_id=agent_id,
        title=title,
        origin=origin,
        origin_ref=origin_ref,
        parent_session_id=parent_session_id,
        thread=thread,
        kind=kind,
    )
    s.add(session)
    await s.flush()
    return session


async def among_in(s: AsyncSession, session_ids: Collection[str]) -> dict[str, Session]:
    """这些会话（已删除的不在结果里），一次查询。"""
    rows = await s.scalars(select(Session).where(Session.id.in_(list(session_ids))))
    return {x.id: x for x in rows.all()}


async def of_agent_in(s: AsyncSession, agent_id: str) -> list[Session]:
    """agent 的全部会话（删除 agent）。"""
    return list((await s.scalars(select(Session).where(Session.agent_id == agent_id))).all())


async def keyed_rows_among(
    s: AsyncSession, origin: Origin, origin_refs: Collection[str], agent_id: str | None = None
) -> dict[tuple[str, str], Session]:
    """按发送方定位会话（inbound.SessionKey）的唯一规则：(发送方身份, agent id) -> 这个发送方给这个 agent 开过的最新
    一个顶层会话，一次查询。给出 agent_id 时只看这个 agent。"""
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


async def keyed_among(s: AsyncSession, session_ids: Collection[str]) -> dict[str, tuple[Origin, str, str]]:
    """其中可以按发送方定位的会话（顶层、带发送方身份）：会话 id -> (来源, 发送方身份, agent id)。"""
    rows = await s.execute(
        select(Session.id, Session.origin, Session.origin_ref, Session.agent_id).where(
            Session.id.in_(list(session_ids)),
            Session.origin_ref.is_not(None),
            Session.kind.in_(TOP_LEVEL),
        )
    )
    return {x: (origin, ref, agent_id) for x, origin, ref, agent_id in rows.all()}


async def threads_of(s: AsyncSession, parent_id: str) -> list[Session]:
    """父会话开出的 thread，按创建顺序。"""
    query = select(Session).where(Session.parent_session_id == parent_id).order_by(Session.created_at)
    return list((await s.scalars(query)).all())


async def thread_in(s: AsyncSession, parent_id: str, agent_id: str, thread: str) -> Session | None:
    """thread 由（父会话, agent, 名字）确定。"""
    return await s.scalar(
        select(Session).where(
            Session.parent_session_id == parent_id, Session.agent_id == agent_id, Session.thread == thread
        )
    )


async def threads_named_in(s: AsyncSession, parent_id: str, thread: str) -> list[Session]:
    """父会话里叫这个名字的 thread（每个 agent 至多一个），按创建顺序。"""
    query = (
        select(Session)
        .where(Session.parent_session_id == parent_id, Session.thread == thread)
        .order_by(Session.created_at)
    )
    return list((await s.scalars(query)).all())


@dataclass(frozen=True)
class Listing:
    """会话列表的筛选：按 agent、来源、发送方身份、关键字筛；都不给就是全部会话。"""

    agent_id: str | None = None
    origins: Sequence[Origin] | None = None
    origin_ref: str | None = None
    # 关键字：标题或任一条消息（pydantic-ai 消息 JSON 原文，含用户消息、回复、工具调用与结果）含有它，不区分 ASCII 大小写
    keyword: str | None = None


def _conditions(listing: Listing) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = []
    if listing.agent_id is not None:
        conditions.append(Session.agent_id == listing.agent_id)
    if listing.origins is not None:
        conditions.append(Session.origin.in_(list(listing.origins)))
    if listing.origin_ref is not None:
        conditions.append(Session.origin_ref == listing.origin_ref)
    if listing.keyword is not None:
        conditions.append(
            or_(
                Session.title.contains(listing.keyword, autoescape=True),
                Session.id.in_(sessions_mentioning(listing.keyword)),
            )
        )
    return conditions


async def listed_ids_in(s: AsyncSession, listing: Listing) -> list[str]:
    return list((await s.scalars(select(Session.id).where(*_conditions(listing)))).all())


async def listed_page_in(
    s: AsyncSession, listing: Listing, first: Collection[str], *, limit: int, offset: int
) -> list[Session]:
    """列表的一页：first 里的会话置顶，其余按最近更新排序。"""
    page = await s.scalars(
        select(Session)
        .where(*_conditions(listing))
        .order_by(Session.id.in_(list(first)).desc(), Session.updated_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(page.all())
