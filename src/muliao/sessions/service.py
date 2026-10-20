"""Session 的查询、删除与在独立事务里接收事件。投递规则在 conversation.receive。"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

import anyio
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
    busy_sessions_of_agent_in,
    check_receivable_in,
    delete_sessions,
    inbound_of,
    open_asks,
    receive,
    receive_lane,
    receive_target,
    threads_of,
)
from muliao.sessions.events import AgentSessionsCleared, Receive
from muliao.sessions.halt import Halted
from muliao.sessions.kinds import LISTED, policy_of
from muliao.sessions.live import LiveRun, LiveRuns
from muliao.sessions.mailbox import latest_run, visible_in
from muliao.sessions.tables import Compaction, Mail, Message, Session
from muliao.sessions.transcript import Item, annotate_asks, to_items

log = logging.getLogger(__name__)

_TITLE_MAX = 60
# 删除 agent 时停止它忙的会话之后，等它们空闲的时限与查看间隔
_IDLE_WAIT_SECONDS = 30.0
_IDLE_POLL_SECONDS = 0.1


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
    def __init__(
        self,
        db: Database,
        queue: TaskQueue,
        live: LiveRuns,
        messaging: Messaging,
        stop_session: Callable[[str], Awaitable[Halted]],
    ):
        self._db = db
        self._queue = queue
        self._live = live
        self._messaging = messaging
        self._stop_session = stop_session

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
        """会话列表只含列出的种类（KindPolicy.listed）；私有 thread 从父会话进入。"""
        query = (
            select(Session)
            .where(Session.kind.in_(LISTED))
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
        messaging.handle(
            Receive, "sessions.receive", self.on_receive, lane=lambda c: receive_lane(receive_target(c))
        )
        messaging.subscribe(
            AgentDeleting,
            "sessions.agent_deleting",
            self.on_agent_deleting,
            lane=lambda e: f"sessions.agent_deleting:{e.agent_id}",
        )

    async def on_receive(self, command: Receive, delivery: Delivery) -> Commit:
        inbound = inbound_of(command)

        async def commit(s: AsyncSession) -> None:
            try:
                await check_receivable_in(s, inbound)
            except NotFound as e:
                # 发送方校验之后会话或 agent 被删除：契约内的变化，这封信不再有收件人
                log.info(
                    "inbound mail dropped: its session or agent is gone",
                    extra={"message_id": delivery.message_id, "origin": command.origin, "reason": str(e)},
                )
                return
            await receive(s, self._queue, inbound)

        return commit

    async def on_agent_deleting(self, event: AgentDeleting, _delivery: Delivery) -> Commit:
        """删除 agent 的流程：删除它的全部会话（连同私有 thread），发布 AgentSessionsCleared（没有会话也发）。

        标记删除之后它的会话不再收到来信（ensure_agent_active_in），但仍可能有会话忙：删除前的检查与标记分属两个
        事务，之间来的信件排上的运行会在标记之后执行、可能开出后台任务或委派；人为重跑它的任务也会让会话变忙。
        忙的会话先停止（每个只停一次；停止也本来是删除的前提：它们的工作随 agent 一起放弃），再在时限内等它们空闲：
        正在执行的运行被取消后，停止事务在它退出之后才执行（清掉占用、结束排队的任务），正在执行的压缩做完才空闲。
        时限内没有空闲（压缩卡住、或停止之后又被人为重跑）就按暂时失败重试，重试时同样先停止再等。"""
        await self._stop_and_wait_idle(event.agent_id)

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

    async def _stop_and_wait_idle(self, agent_id: str) -> None:
        stopped: set[str] = set()
        with anyio.move_on_after(_IDLE_WAIT_SECONDS):
            while True:
                async with self._db.transaction() as s:
                    busy = await busy_sessions_of_agent_in(s, self._queue, agent_id)
                if not busy:
                    return
                for session_id in sorted(set(busy) - stopped):
                    halted = await self._stop_session(session_id)
                    stopped.add(session_id)
                    log.info(
                        "busy session of a deleted agent stopped",
                        extra={"agent": agent_id, "session_id": session_id, "halted": halted},
                    )
                await anyio.sleep(_IDLE_POLL_SECONDS)
        log.warning(
            "sessions of a deleted agent still busy after stopping them",
            extra={"agent": agent_id, "waited_seconds": _IDLE_WAIT_SECONDS},
        )

    async def delete(self, session_id: str) -> None:
        """用户删除会话（规则见 conversation.delete_sessions）。归父会话所有的 thread 随它删除，不能单独删除。"""
        async with self._db.transaction() as s:
            session = await self._get(s, session_id)
            if policy_of(session.kind).owned_by_parent:
                raise Conflict(
                    f"session {session_id} is a {session.kind} owned by the session that opened it; "
                    "delete that session instead"
                )
            await delete_sessions(s, self._queue, self._messaging, [session])


async def _history_in(s: AsyncSession, session_id: str) -> list[tuple[int, ModelMessage]]:
    """完整原始历史（不受压缩影响），附消息 id，按保存顺序。"""
    rows = (
        await s.scalars(select(Message).where(Message.session_id == session_id).order_by(Message.id))
    ).all()
    return list(zip([r.id for r in rows], load_messages(r.payload for r in rows), strict=True))
