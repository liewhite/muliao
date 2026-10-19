"""Session 的查询、删除与在独立事务里接收事件。投递规则在 conversation.receive。"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic_ai.messages import ModelMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.db import Database
from muliao.errors import NotFound
from muliao.models import Compaction, Message, Origin, Session
from muliao.runtime.codec import load_messages
from muliao.runtime.conversation import Inbound, Received, delete_sessions, receive, threads_of
from muliao.runtime.tasks import TaskQueue

_TITLE_MAX = 60


def default_title(prompt: str) -> str:
    first = prompt.strip().splitlines()[0] if prompt.strip() else ""
    return first if len(first) <= _TITLE_MAX else first[: _TITLE_MAX - 1] + "…"


class SessionService:
    def __init__(self, db: Database, queue: TaskQueue):
        self._db = db
        self._queue = queue

    async def receive(self, inbound: Inbound) -> Received:
        """在独立事务里接收一个事件（规则见 conversation.receive）。"""
        async with self._db.transaction() as s:
            return await receive(s, self._queue, inbound)

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
            return await threads_of(s, parent_id)

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
