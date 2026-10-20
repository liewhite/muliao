"""Session 的查询、删除与在独立事务里接收事件。投递规则在 conversation.receive。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pydantic_ai.messages import ModelMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.bus import LiveRun, LiveRuns
from muliao.db import Database
from muliao.delivery.base import mark_session_read_in
from muliao.errors import Conflict, NotFound
from muliao.models import Compaction, Mail, Message, Origin, Session, Task, TaskStatus
from muliao.runtime.codec import load_messages
from muliao.runtime.conversation import (
    Inbound,
    Received,
    answerable_questions,
    delete_sessions,
    receive,
    threads_of,
)
from muliao.runtime.mailbox import latest_run, visible_in
from muliao.runtime.questions import Question
from muliao.runtime.tasks import TaskQueue

_TITLE_MAX = 60


def default_title(prompt: str) -> str:
    first = prompt.strip().splitlines()[0] if prompt.strip() else ""
    return first if len(first) <= _TITLE_MAX else first[: _TITLE_MAX - 1] + "…"


@dataclass(frozen=True)
class SessionView:
    """会话页看到的会话（读模型）：一次取齐，与实时快照对得上。"""

    session: Session
    # 完整原始历史（不受压缩影响）附消息 id；正在进行的那一段已经保存的部分由实时快照展示，这里不重复（bus.LiveRuns）
    history: list[tuple[int, ModelMessage]]
    compactions: list[Compaction]
    # 显示在历史之后的信件：排队中的，以及最近一次停止丢弃的
    mail: list[Mail]
    # 最近一次运行，还没正常结束时
    run: Task | None
    threads: list[Session]
    # 现在能回答的提问（conversation.answerable_questions）
    questions: dict[str, list[Question]]
    live: LiveRun | None


class SessionService:
    def __init__(self, db: Database, queue: TaskQueue, live: LiveRuns):
        self._db = db
        self._queue = queue
        self._live = live

    async def detail(self, session_id: str) -> SessionView:
        # 先取快照再读库：快照这一段已在展示的消息不在历史里重复；之间换了段时，新保存的消息照旧由旧快照展示，
        # saved 事件随后让前端重新拉取
        live = self._live.snapshot(session_id)
        async with self._db.transaction() as s:
            session = await self._get(s, session_id)
            rows = (
                await s.scalars(select(Message).where(Message.session_id == session_id).order_by(Message.id))
            ).all()
            shown = [
                r
                for r in rows
                if live is None or live.after_message_id is None or r.id <= live.after_message_id
            ]
            compactions = list(
                (
                    await s.scalars(
                        select(Compaction).where(Compaction.session_id == session_id).order_by(Compaction.id)
                    )
                ).all()
            )
            latest = await latest_run(s, session_id)
            return SessionView(
                session=session,
                history=list(
                    zip([r.id for r in shown], load_messages(r.payload for r in shown), strict=True)
                ),
                compactions=compactions,
                mail=await visible_in(s, session_id),
                run=None if latest is None or latest.status == TaskStatus.DONE else latest,
                threads=await threads_of(s, session_id),
                questions=await answerable_questions(s, session_id),
                live=live,
            )

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

    async def mark_read(self, session_id: str) -> None:
        async with self._db.transaction() as s:
            (await self._get(s, session_id)).unread = False
            await mark_session_read_in(s, session_id)

    async def delete(self, session_id: str) -> None:
        """用户删除会话（规则见 conversation.delete_sessions）。私有 thread 属于发问的会话，随它删除，不能单独删除。"""
        async with self._db.transaction() as s:
            session = await self._get(s, session_id)
            if session.private:
                raise Conflict(
                    f"session {session_id} is a private thread; delete the session that asked it instead"
                )
            await delete_sessions(s, self._queue, [session])
