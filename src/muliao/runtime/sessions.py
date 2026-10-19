"""Session 生命周期。所有触发源都通过这里创建 session 并投递 turn。"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic_ai.messages import ModelMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.db import Database, utcnow
from muliao.errors import Conflict, InvalidInput, NotFound
from muliao.ids import new_id
from muliao.models import Agent, Compaction, DeliveryChannel, Message, Origin, Session, Task, TaskKind
from muliao.runtime.codec import load_messages
from muliao.runtime.tasks import TaskQueue, running_task_ids, turn_lane

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
        agent_name: str,
        prompt: str,
        origin: Origin,
        origin_ref: str | None,
        delivery: DeliveryChannel,
        delivery_target: str | None = None,
        title: str | None = None,
    ) -> Session:
        if await s.get(Agent, agent_name) is None:
            raise NotFound(f"agent {agent_name!r} not found")
        session = Session(
            id=new_id(),
            agent_name=agent_name,
            title=title or default_title(prompt),
            origin=origin,
            origin_ref=origin_ref,
            delivery=delivery,
            delivery_target=delivery_target,
        )
        s.add(session)
        self.enqueue_turn_in(s, session, prompt)
        return session

    def enqueue_turn_in(self, s: AsyncSession, session: Session, prompt: str) -> Task:
        if not prompt.strip():
            raise InvalidInput("prompt must not be empty")
        session.updated_at = utcnow()
        return self._queue.enqueue(
            s,
            kind=TaskKind.TURN,
            lane=turn_lane(session.id),
            agent_name=session.agent_name,
            session_id=session.id,
            payload={"prompt": prompt},
        )

    async def start(self, **kwargs) -> Session:  # noqa: ANN003  参数同 create_in
        async with self._db.transaction() as s:
            return await self.create_in(s, **kwargs)

    async def reply(self, session_id: str, prompt: str) -> Task:
        async with self._db.transaction() as s:
            session = await self._get(s, session_id)
            return self.enqueue_turn_in(s, session, prompt)

    async def _get(self, s: AsyncSession, session_id: str) -> Session:
        session = await s.get(Session, session_id)
        if session is None:
            raise NotFound(f"session {session_id!r} not found")
        return session

    async def get(self, session_id: str) -> Session:
        async with self._db.transaction() as s:
            return await self._get(s, session_id)

    async def list(
        self, *, agent_name: str | None, origins: Sequence[Origin] | None, limit: int, offset: int
    ) -> list[Session]:
        query = select(Session).order_by(Session.updated_at.desc()).limit(limit).offset(offset)
        if agent_name is not None:
            query = query.where(Session.agent_name == agent_name)
        if origins is not None:
            query = query.where(Session.origin.in_(list(origins)))
        async with self._db.transaction() as s:
            return list((await s.scalars(query)).all())

    async def find_latest(self, origin: Origin, origin_ref: str) -> Session | None:
        async with self._db.transaction() as s:
            return await s.scalar(
                select(Session)
                .where(Session.origin == origin, Session.origin_ref == origin_ref)
                .order_by(Session.created_at.desc())
                .limit(1)
            )

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

    async def compactions(self, session_id: str) -> list[Compaction]:
        async with self._db.transaction() as s:
            query = select(Compaction).where(Compaction.session_id == session_id).order_by(Compaction.id)
            return list((await s.scalars(query)).all())

    async def mark_read(self, session_id: str) -> None:
        async with self._db.transaction() as s:
            (await self._get(s, session_id)).unread = False

    async def delete(self, session_id: str) -> None:
        """删除 session 及其消息和未执行的 task；有 task 正在运行时拒绝，避免运行结果无处提交。"""
        async with self._db.transaction() as s:
            session = await self._get(s, session_id)
            if running := await running_task_ids(s, session_id=session_id):
                raise Conflict(f"session {session_id} has running tasks {running}; wait for them to finish")
            await s.delete(session)
