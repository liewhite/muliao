"""Session 的查询、删除与在独立事务里接收事件。投递规则在 inbound.receive。"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from pydantic_ai.messages import ModelMessage
from sqlalchemy import ColumnElement, and_, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.errors import Conflict, NotFound
from muliao.platform.db import Database
from muliao.platform.messaging import Delivery, Messaging
from muliao.platform.tables import Task, TaskKind, TaskStatus
from muliao.platform.tasks import Commit, TaskQueue, no_commit
from muliao.platform.tooling import Origin
from muliao.sessions.activity import WorkState, work_states_in
from muliao.sessions.codec import load_messages
from muliao.sessions.deletion import delete_sessions
from muliao.sessions.events import Receive, SessionRead, StopThread
from muliao.sessions.halt import Halted
from muliao.sessions.inbound import (
    Inbound,
    Received,
    check_receivable_in,
    inbound_of,
    open_asks,
    receive,
    receive_lane_of,
)
from muliao.sessions.kinds import LISTED, policy_of
from muliao.sessions.live import LiveRun, LiveRuns
from muliao.sessions.mailbox import latest_run, visible_in
from muliao.sessions.tables import Compaction, Mail, Message, Session
from muliao.sessions.threads import threads_of
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
    def __init__(
        self,
        db: Database,
        queue: TaskQueue,
        live: LiveRuns,
        messaging: Messaging,
        stop_session: Callable[[str], Awaitable[Halted]],
        run_now: Callable[[int], Awaitable[None]],
    ):
        self._db = db
        self._queue = queue
        self._live = live
        self._messaging = messaging
        self._stop_session = stop_session
        # 马上执行一个已提交的投递任务（Worker.run_now）
        self._run_now = run_now

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
        """在独立事务里接收一个事件（规则见 inbound.receive）。"""
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
        self,
        *,
        agent_id: str | None,
        origins: Sequence[Origin] | None,
        limit: int,
        offset: int,
        origin_ref: str | None = None,
    ) -> list[Session]:
        """会话列表只含列出的种类（KindPolicy.listed）；私有 thread 从父会话进入。正在运行的会话置顶，其余按最近
        更新排序。

        origin_ref 是发起方在它那个 origin 里的身份（触发器的 id），用来列出某个发起方开过的会话。
        """
        running = exists().where(_running_run(), Task.session_id == Session.id)
        query = (
            select(Session)
            .where(Session.kind.in_(LISTED))
            .order_by(running.desc(), Session.updated_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if agent_id is not None:
            query = query.where(Session.agent_id == agent_id)
        if origins is not None:
            query = query.where(Session.origin.in_(list(origins)))
        if origin_ref is not None:
            query = query.where(Session.origin_ref == origin_ref)
        async with self._db.transaction() as s:
            return list((await s.scalars(query)).all())

    async def work_states(self, session_ids: Sequence[str]) -> dict[str, WorkState]:
        """这些会话的工作状态（会话列表每一行的标记，与 issue 看板、触发器同一条规则）。"""
        async with self._db.transaction() as s:
            return await work_states_in(s, self._queue, session_ids)

    async def threads(self, parent_id: str) -> list[Session]:
        """父会话开出的 thread（含私有的），按创建顺序。"""
        async with self._db.transaction() as s:
            await self._get(s, parent_id)
            return await threads_of(s, parent_id)

    async def history_with_ids(self, session_id: str) -> list[tuple[int, ModelMessage]]:
        """完整原始历史（不受压缩影响），附消息 id。"""
        async with self._db.transaction() as s:
            await self._get(s, session_id)
            return await _history_in(s, session_id)

    async def mark_read(self, session_id: str) -> None:
        """用户看过了会话：清掉会话的未读，同一事务发布 SessionRead（通知领域据此把指向它的通知标为已读）。提交后
        马上投递，通常返回时界面重新取的未读数已经反映；同一 lane 上还有更早的投递时留给 worker 稍后处理（界面在
        事件投递完成时再取未读数）。订阅者的处理必须短小（在请求里执行）。"""
        async with self._db.transaction() as s:
            (await self._get(s, session_id)).unread = False
            published = self._messaging.publish_in(s, SessionRead(session_id=session_id))
        for task in published.deliveries:
            await self._run_now(task.id)

    def subscribe(self, messaging: Messaging) -> None:
        messaging.handle(StopThread, "sessions.stop_thread", self.on_stop_thread)
        messaging.handle(Receive, "sessions.receive", self.on_receive, lane=receive_lane_of)

    async def on_stop_thread(self, command: StopThread, _delivery: Delivery) -> Commit:
        """一个 thread 做的委派的发起方被停止了：停止它（已被删除时是 GONE，什么都不做）。"""
        halted = await self._stop_session(command.session_id)
        log.info(
            "thread of a stopped session stopped", extra={"session_id": command.session_id, "halted": halted}
        )
        return no_commit

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

    async def delete(self, session_id: str) -> None:
        """用户删除会话（规则见 deletion.delete_sessions）。归父会话所有的 thread 随它删除，不能单独删除。"""
        async with self._db.transaction() as s:
            session = await self._get(s, session_id)
            if policy_of(session.kind).owned_by_parent:
                raise Conflict(
                    f"session {session_id} is a {session.kind} owned by the session that opened it; "
                    "delete that session instead"
                )
            await delete_sessions(s, self._queue, self._messaging, [session])


def _running_run() -> ColumnElement[bool]:
    """进行中的运行任务（不含排队的运行与压缩）。"""
    return and_(Task.kind == TaskKind.RUN, Task.status == TaskStatus.RUNNING)


async def _history_in(s: AsyncSession, session_id: str) -> list[tuple[int, ModelMessage]]:
    """完整原始历史（不受压缩影响），附消息 id，按保存顺序。"""
    rows = (
        await s.scalars(select(Message).where(Message.session_id == session_id).order_by(Message.id))
    ).all()
    return list(zip([r.id for r in rows], load_messages(r.payload for r in rows), strict=True))
