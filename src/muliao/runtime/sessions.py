"""Session 生命周期。所有触发源都通过这里创建 session 并投递 turn。"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic_ai.messages import ModelMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.db import Database
from muliao.errors import Conflict, NotFound
from muliao.ids import new_id
from muliao.models import Agent, Compaction, Message, Origin, ReplyTo, Session, Task
from muliao.runtime.codec import load_messages
from muliao.runtime.conversation import delete_sessions, enqueue_turn
from muliao.runtime.tasks import TaskQueue

_TITLE_MAX = 60


def default_title(prompt: str) -> str:
    first = prompt.strip().splitlines()[0] if prompt.strip() else ""
    return first if len(first) <= _TITLE_MAX else first[: _TITLE_MAX - 1] + "…"


class SessionService:
    def __init__(self, db: Database, queue: TaskQueue):
        self._db = db
        self._queue = queue

    async def create_in(
        self,
        s: AsyncSession,
        *,
        agent_id: str,
        prompt: str,
        origin: Origin,
        origin_ref: str | None,
        reply_to: ReplyTo,
        title: str | None = None,
    ) -> Session:
        """新建顶层 session（用户或 cron 发起）。委派产生的子会话由 conversation.Threads 创建。"""
        if await s.get(Agent, agent_id) is None:
            raise NotFound(f"agent {agent_id!r} not found")
        session = Session(
            id=new_id(),
            agent_id=agent_id,
            title=title or default_title(prompt),
            origin=origin,
            origin_ref=origin_ref,
        )
        s.add(session)
        enqueue_turn(s, self._queue, session, prompt, reply_to)
        return session

    async def start(self, **kwargs) -> Session:  # noqa: ANN003  参数同 create_in
        async with self._db.transaction() as s:
            return await self.create_in(s, **kwargs)

    async def reply(self, session_id: str, prompt: str) -> Task:
        """用户在 session 里追问。私有 thread 只属于发起它的 agent，用户只读。"""
        async with self._db.transaction() as s:
            session = await self._get(s, session_id)
            if session.private:
                raise Conflict(f"session {session_id} is a private thread of another agent and is read-only")
            return enqueue_turn(s, self._queue, session, prompt, ReplyTo.USER)

    async def _get(self, s: AsyncSession, session_id: str) -> Session:
        session = await s.get(Session, session_id)
        if session is None:
            raise NotFound(f"session {session_id!r} not found")
        return session

    async def get(self, session_id: str) -> Session:
        async with self._db.transaction() as s:
            return await self._get(s, session_id)

    async def list(
        self, *, agent_id: str | None, origins: Sequence[Origin] | None, limit: int, offset: int
    ) -> list[Session]:
        """会话列表只含用户可见的会话；私有 thread 从父会话进入。"""
        query = (
            select(Session)
            .where(Session.private.is_(False))
            .order_by(Session.updated_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if agent_id is not None:
            query = query.where(Session.agent_id == agent_id)
        if origins is not None:
            query = query.where(Session.origin.in_(list(origins)))
        async with self._db.transaction() as s:
            return list((await s.scalars(query)).all())

    async def threads(self, parent_id: str) -> list[Session]:
        """父会话开出的 thread（含私有的），按创建顺序。"""
        async with self._db.transaction() as s:
            await self._get(s, parent_id)
            query = select(Session).where(Session.parent_session_id == parent_id).order_by(Session.created_at)
            return list((await s.scalars(query)).all())

    async def history(self, session_id: str) -> list[ModelMessage]:
        return [m for _, m in await self.history_with_ids(session_id)]

    async def history_with_ids(self, session_id: str) -> list[tuple[int, ModelMessage]]:
        """完整原始历史（不受压缩影响），附消息 id。"""
        async with self._db.transaction() as s:
            await self._get(s, session_id)
            rows = (
                await s.scalars(select(Message).where(Message.session_id == session_id).order_by(Message.id))
            ).all()
        return list(zip([r.id for r in rows], load_messages(r.payload for r in rows), strict=True))

    async def turns_with_messages(self, session_id: str) -> set[int]:
        """已提交过消息的轮次（含挂起中的）。"""
        async with self._db.transaction() as s:
            query = select(Message.task_id).where(Message.session_id == session_id).distinct()
            return set((await s.scalars(query)).all())

    async def compactions(self, session_id: str) -> list[Compaction]:
        async with self._db.transaction() as s:
            query = select(Compaction).where(Compaction.session_id == session_id).order_by(Compaction.id)
            return list((await s.scalars(query)).all())

    async def mark_read(self, session_id: str) -> None:
        async with self._db.transaction() as s:
            (await self._get(s, session_id)).unread = False

    async def delete(self, session_id: str) -> None:
        """删除 session（规则见 conversation.delete_sessions）。"""
        async with self._db.transaction() as s:
            await delete_sessions(s, self._queue, [await self._get(s, session_id)])
