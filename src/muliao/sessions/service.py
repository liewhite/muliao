"""Session 的查询、删除与在独立事务里接收事件。投递规则在 conversation.receive。"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic_ai.messages import ModelMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.events import AgentDeleting
from muliao.errors import Conflict, NotFound, TransientError
from muliao.platform.db import Database
from muliao.platform.messaging import Delivery, Messaging
from muliao.platform.tables import Task, TaskStatus
from muliao.platform.tasks import Commit, TaskQueue
from muliao.platform.tooling import Origin
from muliao.sessions.codec import load_messages
from muliao.sessions.conversation import (
    Inbound,
    Received,
    delete_sessions,
    open_asks,
    receive,
    threads_of,
)
from muliao.sessions.events import AgentSessionsCleared
from muliao.sessions.live import LiveRun, LiveRuns
from muliao.sessions.mailbox import latest_run, visible_in
from muliao.sessions.tables import Compaction, Mail, Message, Session
from muliao.sessions.transcript import Item, annotate_asks, to_items

log = logging.getLogger(__name__)

_TITLE_MAX = 60


def default_title(prompt: str) -> str:
    first = prompt.strip().splitlines()[0] if prompt.strip() else ""
    return first if len(first) <= _TITLE_MAX else first[: _TITLE_MAX - 1] + "…"


@dataclass(frozen=True)
class SessionView:
    """会话页看到的会话（读模型）：一次取齐，与实时快照对得上。"""

    session: Session
    # 完整原始历史（不受压缩影响）的展示条目，带来源消息 id 与提问状态；正在进行的那一段已经保存的部分由实时快照
    # 展示，这里不重复（sessions.live.LiveRuns）
    items: list[Item]
    compactions: list[Compaction]
    # 显示在历史之后的信件：排队中的，以及最近一次停止丢弃的
    mail: list[Mail]
    # 最近一次运行，还没正常结束时
    run: Task | None
    threads: list[Session]
    live: LiveRun | None


class SessionService:
    def __init__(self, db: Database, queue: TaskQueue, live: LiveRuns, messaging: Messaging):
        self._db = db
        self._queue = queue
        self._live = live
        self._messaging = messaging

    async def detail(self, session_id: str) -> SessionView:
        # 先取快照再读库：快照这一段已在展示的消息不在历史里重复；之间换了段时，新保存的消息照旧由旧快照展示，
        # saved 事件随后让前端重新拉取
        live = self._live.snapshot(session_id)
        async with self._db.transaction() as s:
            session = await self._get(s, session_id)
            history = await _history_in(s, session_id)
            asks = await open_asks(s, session_id)
            items = annotate_asks(
                [
                    item.model_copy(update={"message_id": message_id})
                    for message_id, message in history
                    if live is None or live.after_message_id is None or message_id <= live.after_message_id
                    for item in to_items([message])
                ],
                answerable=asks.answerable.keys(),
                submitted=asks.submitted,
            )
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
                items=items,
                compactions=compactions,
                mail=await visible_in(s, session_id),
                run=None if latest is None or latest.status == TaskStatus.DONE else latest,
                threads=await threads_of(s, session_id),
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
            return await _history_in(s, session_id)

    async def mark_read(self, session_id: str) -> None:
        async with self._db.transaction() as s:
            (await self._get(s, session_id)).unread = False

    def subscribe(self, messaging: Messaging) -> None:
        messaging.subscribe(
            AgentDeleting,
            "sessions.agent_deleting",
            self.on_agent_deleting,
            lane=lambda e: f"agent:{e.agent_id}",
        )

    async def on_agent_deleting(self, event: AgentDeleting, _delivery: Delivery) -> Commit:
        """删除 agent 的流程：删除它的全部会话，发布 AgentSessionsCleared（没有会话也发）。标记删除时它的会话都不忙，
        之后也不会再收到来信（ensure_agent_active_in），所以这里通常一次删完。仍有会话忙只可能是人为重跑了它的失败
        任务：按暂时失败重试，重试用完时投递记为失败，在「任务」页可见。"""

        async def commit(s: AsyncSession) -> None:
            sessions = list(
                (await s.scalars(select(Session).where(Session.agent_id == event.agent_id))).all()
            )
            try:
                await delete_sessions(s, self._queue, self._messaging, sessions)
            except Conflict as e:
                log.warning(
                    "deleted agent still has a busy session, will retry", extra={"agent": event.agent_id}
                )
                raise TransientError(f"agent {event.agent_id} still has a busy session: {e}") from e
            self._messaging.publish_in(s, AgentSessionsCleared(agent_id=event.agent_id))
            log.info(
                "sessions of a deleted agent removed",
                extra={"agent": event.agent_id, "session_ids": [x.id for x in sessions]},
            )

        return commit

    async def delete(self, session_id: str) -> None:
        """用户删除会话（规则见 conversation.delete_sessions）。私有 thread 属于发问的会话，随它删除，不能单独删除。"""
        async with self._db.transaction() as s:
            session = await self._get(s, session_id)
            if session.private:
                raise Conflict(
                    f"session {session_id} is a private thread; delete the session that asked it instead"
                )
            await delete_sessions(s, self._queue, self._messaging, [session])


async def _history_in(s: AsyncSession, session_id: str) -> list[tuple[int, ModelMessage]]:
    """完整原始历史（不受压缩影响），附消息 id，按保存顺序。"""
    rows = (
        await s.scalars(select(Message).where(Message.session_id == session_id).order_by(Message.id))
    ).all()
    return list(zip([r.id for r in rows], load_messages(r.payload for r in rows), strict=True))
