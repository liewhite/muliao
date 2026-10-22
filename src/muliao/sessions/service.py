"""Session 的查询、删除与在独立事务里接收事件。投递规则在 inbound.receive。"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.errors import Conflict, NotFound
from muliao.platform.db import Database
from muliao.platform.messaging import Delivery, Messaging
from muliao.platform.tables import Task, TaskStatus
from muliao.platform.tasks import Commit, TaskQueue, no_commit
from muliao.sessions.activity import WorkState, work_states_in
from muliao.sessions.codec import load_messages
from muliao.sessions.compaction import Compactor
from muliao.sessions.deletion import delete_sessions
from muliao.sessions.events import Receive, SessionRead, StopThread
from muliao.sessions.halt import Halted, StopSession
from muliao.sessions.inbound import (
    Inbound,
    Received,
    inbound_of,
    open_asks,
    receive,
    receive_into,
    receive_lane_of,
    target_in,
)
from muliao.sessions.kinds import LISTED, policy_of
from muliao.sessions.live import LiveRun, LiveRuns
from muliao.sessions.mailbox import latest_run, visible_in
from muliao.sessions.origins import Origin
from muliao.sessions.tables import Compaction, Mail, Message, Session
from muliao.sessions.threads import threads_of
from muliao.sessions.transcript import Item, annotate_asks, to_items

log = logging.getLogger(__name__)


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
    # 工作状态（与会话列表同一条规则）：不是 idle 就有可停的事（运行、排队的来信、后台任务与委派、挂起的提问）
    state: WorkState


@dataclass(frozen=True)
class ListedSession:
    """会话列表的一行：会话与排序时用的工作状态（同一次计算，置顶与标记对得上）。"""

    session: Session
    state: WorkState


class SessionService:
    def __init__(
        self,
        db: Database,
        queue: TaskQueue,
        live: LiveRuns,
        messaging: Messaging,
        stop_session: StopSession,
        compactor: Compactor,
        run_now: Callable[[int], Awaitable[None]],
    ):
        self._db = db
        self._queue = queue
        self._live = live
        self._messaging = messaging
        self._stop_session = stop_session
        self._compactor = compactor
        # 马上执行一个已提交的投递任务（Worker.run_now）
        self._run_now = run_now

    async def detail(self, session_id: str) -> SessionView:
        # 先取快照再读库：快照这一段已在展示的消息不在历史里重复；之间换了段时，新保存的消息照旧由旧快照展示，
        # saved 事件随后让前端重新拉取
        live = self._live.snapshot(session_id)
        # 事务（全局写锁）里只取原始行；反序列化与转成条目在事务之外：运行每保存一步前端都会重新拉取，长会话的这部分
        # 不该挡住别的事务
        async with self._db.transaction() as s:
            session = await self._get(s, session_id)
            rows = (
                await s.execute(
                    select(Message.id, Message.payload)
                    .where(Message.session_id == session_id)
                    .order_by(Message.id)
                )
            ).all()
            asks = await open_asks(s, session_id)
            compactions = list(
                (
                    await s.scalars(
                        select(Compaction).where(Compaction.session_id == session_id).order_by(Compaction.id)
                    )
                ).all()
            )
            latest = await latest_run(s, session_id)
            mail = await visible_in(s, session_id)
            threads = await threads_of(s, session_id)
            state = (await work_states_in(s, self._queue, [session_id]))[session_id]
        shown = [
            (message_id, payload)
            for message_id, payload in rows
            if live is None or live.after_message_id is None or message_id <= live.after_message_id
        ]
        items = annotate_asks(
            [
                item.model_copy(update={"message_id": message_id})
                for (message_id, _), message in zip(shown, load_messages(p for _, p in shown), strict=True)
                for item in to_items([message])
            ],
            answerable=asks.answerable.keys(),
            submitted=asks.submitted,
        )
        return SessionView(
            session=session,
            items=items,
            compactions=compactions,
            mail=mail,
            run=None if latest is None or latest.status == TaskStatus.DONE else latest,
            threads=threads,
            live=live,
            state=state,
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

    async def stop(self, session_id: str) -> Halted:
        """用户停止会话：结束正在进行的运行（已经产生的部分照常保存）、清空信箱，连带停下它发起的异步工作
        （sessions.halt）。会话不存在时报 NotFound。"""
        await self.get(session_id)
        return await self._stop_session(session_id)

    async def compact(self, session_id: str) -> bool:
        """用户手动压缩上下文（sessions.compaction.Compactor.request）：返回是否新排了一次。"""
        return await self._compactor.request(session_id)

    async def list(
        self,
        *,
        agent_id: str | None,
        origins: Sequence[Origin] | None,
        limit: int,
        offset: int,
        origin_ref: str | None = None,
    ) -> list[ListedSession]:
        """会话列表只含列出的种类（KindPolicy.listed）；私有 thread 从父会话进入。工作状态不是 idle 的会话（在运行、
        在等后台任务/委派/用户回答，与 issue 看板、触发器同一条规则）置顶，其余按最近更新排序。

        置顶看的是筛选后的全部会话（不只是这一页），所以第一页就含全部进行中的会话：工作状态的查询次数与会话数无关，
        代价是每个会话读一条最后的消息（挂起的提问）。origin_ref 是发起方在它那个 origin 里的身份（触发器的 id），
        用来列出某个发起方开过的会话。
        """
        conditions = [Session.kind.in_(LISTED)]
        if agent_id is not None:
            conditions.append(Session.agent_id == agent_id)
        if origins is not None:
            conditions.append(Session.origin.in_(list(origins)))
        if origin_ref is not None:
            conditions.append(Session.origin_ref == origin_ref)
        async with self._db.transaction() as s:
            ids = list((await s.scalars(select(Session.id).where(*conditions))).all())
            states = await work_states_in(s, self._queue, ids)
            active = [x for x in ids if states[x] != WorkState.IDLE]
            page = await s.scalars(
                select(Session)
                .where(*conditions)
                .order_by(Session.id.in_(active).desc(), Session.updated_at.desc())
                .limit(limit)
                .offset(offset)
            )
            return [ListedSession(x, states[x.id]) for x in page.all()]

    async def mark_read(self, session_id: str) -> None:
        """用户看过了会话：清掉会话的未读，同一事务发布 SessionRead（通知领域据此把指向它的通知标为已读）。提交后
        马上投递，通常返回时界面重新取的未读数已经反映；同一 lane 上还有更早的投递时留给 worker 稍后处理（界面在
        事件投递完成时再取未读数）。订阅者的处理必须短小（在请求里执行）。"""
        async with self._db.transaction() as s:
            (await self._get(s, session_id)).unread = False
            deliveries = self._messaging.publish_in(s, SessionRead(session_id=session_id))
        for task in deliveries:
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
                target = await target_in(s, inbound)
            except NotFound as e:
                # 发送方校验之后会话或 agent 被删除：契约内的变化，这封信不再有收件人
                log.info(
                    "inbound mail dropped: its session or agent is gone",
                    extra={"message_id": delivery.message_id, "origin": command.origin, "reason": str(e)},
                )
                return
            await receive_into(s, self._queue, inbound, *target)

        return commit

    async def delete(self, session_id: str) -> None:
        """用户删除会话（规则见 deletion.delete_sessions）。归父会话所有的 thread 随它删除，不能单独删除。"""
        async with self._db.transaction() as s:
            session = await self._get(s, session_id)
            if not policy_of(session.kind).deletable:
                raise Conflict(
                    f"session {session_id} is a {session.kind} owned by the session that opened it; "
                    "delete that session instead"
                )
            await delete_sessions(s, self._queue, self._messaging, [session])
