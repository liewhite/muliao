"""会话事件队列：交给某个会话的 agent、等下一轮一起处理的事件（后台任务完成、运行中的检查……）。

规则（设计见 docs/architecture.md「会话事件」）：
- 事件按到达顺序排在会话的队列里（session_events）。一轮开始时取走会话里所有还没处理的事件，和这一轮的用户消息
  按到达时间排好，一起交给模型；这一轮提交时记下处理掉它们的 turn（consumed_by）。
- 会话在等用户回答提问时事件留着；用户回答或跳过（发新消息）的那一轮一起带上。
- 事件需要一轮来处理时投递一轮（dispatch_in）：会话里没有排队或运行中的轮次、没有挂起的提问，并且它到达之后
  还没有新建过任何一轮。最后一条保证「处理事件的那一轮失败了」不会无限重新投递：那些事件已经交给了那一轮，
  它被重试、或之后任何一轮（用户的新消息、更新的事件）开始时都会带上它们。
- dispatch_in 有三个调用方，共用这一条规则：事件到达时、一轮提交时（处理运行期间到达的事件）、以及定期对账
  （EventDispatcher，兜住失败与重启这类没有及时投递的情况）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.conventions import format_event
from muliao.db import Database
from muliao.errors import Conflict, NotFound
from muliao.models import ReplyTo, Session, SessionEvent, Task, TaskKind, TaskStatus
from muliao.runtime.conversation import TurnPayload
from muliao.runtime.questions import pending_in_session
from muliao.runtime.tasks import TaskQueue, turn_lane

log = logging.getLogger(__name__)

_SOURCE_MAX = 100
_RECONCILE_SECONDS = 30.0


async def post_in(
    s: AsyncSession, queue: TaskQueue, session_id: str, *, source: str, content: str, reply_to: ReplyTo
) -> SessionEvent:
    """在调用方事务里把一条事件排进会话的队列，需要时投递一轮来处理它。"""
    if not source or len(source) > _SOURCE_MAX or '"' in source:
        raise ValueError(f"event source must be 1-{_SOURCE_MAX} characters without quotes, got {source!r}")
    if reply_to == ReplyTo.AGENT:
        # AGENT 只属于 ask_agent 的内联轮次，事件不经过那里
        raise ValueError("events cannot reply to an agent")
    session = await s.get(Session, session_id)
    if session is None:
        raise NotFound(f"session {session_id!r} not found")
    if session.private:
        # 私有 thread 只在发问方那一轮里内联运行，没有自己的队列轮次来处理事件
        raise Conflict(f"session {session_id} is a private thread; it cannot receive events")
    event = SessionEvent(session_id=session_id, source=source, content=content, reply_to=reply_to)
    s.add(event)
    await s.flush()
    log.info("session event posted", extra={"session_id": session_id, "event_id": event.id, "source": source})
    await dispatch_in(s, queue, session)
    return event


async def queued_in(s: AsyncSession, session_id: str) -> list[SessionEvent]:
    """会话里还没处理的事件，按到达顺序。"""
    rows = await s.scalars(
        select(SessionEvent)
        .where(SessionEvent.session_id == session_id, SessionEvent.consumed_by.is_(None))
        .order_by(SessionEvent.id)
    )
    return list(rows.all())


async def dispatch_in(
    s: AsyncSession, queue: TaskQueue, session: Session, *, committing: int | None = None
) -> Task | None:
    """有事件在等一轮来处理时投递一轮（规则见模块说明）；返回投递的 turn，不需要时为 None。

    committing：正在提交的那一轮（在它的提交事务里调用时）。队列在同一个事务里稍后才把它记为完成，此刻它还是
    running，不能因此判定会话忙。"""
    newest = await s.scalar(
        select(func.max(SessionEvent.created_at)).where(
            SessionEvent.session_id == session.id, SessionEvent.consumed_by.is_(None)
        )
    )
    if newest is None:
        return None
    lane = turn_lane(session.id)
    turns = select(Task).where(Task.lane == lane, Task.kind == TaskKind.TURN)
    busy = turns.where(Task.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]))
    if committing is not None:
        busy = busy.where(Task.id != committing)
    if await s.scalar(select(busy.exists())):
        # 排队的那一轮开始时会带上它们；运行中的那一轮提交时会再来这里
        return None
    latest = await s.scalar(
        select(func.max(Task.created_at)).where(Task.lane == lane, Task.kind == TaskKind.TURN)
    )
    # 「交给过」由两张表的 created_at（同一个时钟 db.utcnow）比较得出，依赖系统时间不回拨
    if latest is not None and newest <= latest:
        # 都已经交给过一轮（它失败了、等重试）：由它或之后的任何一轮带上，不重复投递
        return None
    if await pending_in_session(s, session.id):
        # 等用户回答或跳过：那一轮会带上它们
        return None
    turn = queue.enqueue(
        s,
        kind=TaskKind.TURN,
        lane=lane,
        agent_id=session.agent_id,
        session_id=session.id,
        # 只有事件的一轮：没有用户消息；推不推送由它带上的事件决定（TurnEvents.reply_to）
        payload=TurnPayload(prompt="", reply_to=ReplyTo.NONE, attachments=[], answers={}).dump(),
    )
    log.info("turn dispatched for session events", extra={"session_id": session.id})
    return turn


@dataclass(frozen=True)
class TurnEvents:
    """一轮带上的事件：在这一轮的用户消息之前到达的、之后到达的（按到达时间排在用户消息两侧）。"""

    ids: tuple[int, ...]
    before: tuple[str, ...]
    after: tuple[str, ...]
    # 任何一条事件要求推送，这一轮的回答就推送给用户
    to_user: bool

    @classmethod
    def of(cls, events: Sequence[SessionEvent], received_at: datetime) -> TurnEvents:
        return cls(
            ids=tuple(e.id for e in events),
            before=tuple(format_event(e.source, e.content) for e in events if e.created_at <= received_at),
            after=tuple(format_event(e.source, e.content) for e in events if e.created_at > received_at),
            to_user=any(e.reply_to == ReplyTo.USER for e in events),
        )

    def reply_to(self, own: ReplyTo) -> ReplyTo:
        """这一轮的回答交给谁：轮次自己的去向，或者带上的事件要求推送给用户。"""
        return ReplyTo.USER if self.to_user and own == ReplyTo.NONE else own


async def take_in(s: AsyncSession, session_id: str, received_at: datetime) -> TurnEvents:
    """一轮开始时取走的事件；received_at 是这一轮的用户消息到达的时间。提交时才记为处理掉（consume_in）。"""
    return TurnEvents.of(await queued_in(s, session_id), received_at)


async def consume_in(s: AsyncSession, ids: Sequence[int], turn_id: int) -> None:
    """一轮提交时记下它处理掉的事件。同一会话的轮次串行执行，取走的事件不可能被别的轮次处理掉。"""
    if not ids:
        return
    result = await s.execute(
        update(SessionEvent)
        .where(SessionEvent.id.in_(ids), SessionEvent.consumed_by.is_(None))
        .values(consumed_by=turn_id)
    )
    if result.rowcount != len(ids):  # type: ignore[attr-defined]
        raise RuntimeError(
            f"turn {turn_id} took events {list(ids)} but only {result.rowcount} were still queued"  # type: ignore[attr-defined]
        )


class EventDispatcher:
    """定期对账：有事件在等、却没有被及时投递一轮的会话（投递时的事务失败、进程重启），按同一条规则补上。"""

    def __init__(self, db: Database, queue: TaskQueue, interval_seconds: float = _RECONCILE_SECONDS):
        self._db = db
        self._queue = queue
        self._interval = interval_seconds

    async def reconcile(self) -> int:
        """补投递的轮数。"""
        async with self._db.transaction() as s:
            waiting = (
                await s.scalars(
                    select(Session)
                    .where(
                        Session.id.in_(
                            select(SessionEvent.session_id).where(SessionEvent.consumed_by.is_(None))
                        )
                    )
                    .order_by(Session.id)
                )
            ).all()
            dispatched = [session.id for session in waiting if await dispatch_in(s, self._queue, session)]
        if dispatched:
            # 正常路径也会走到这里（不可重试地失败的一轮运行期间到达的事件），所以是 info；补了什么仍然可查
            log.info(
                "reconcile dispatched turns for waiting session events", extra={"session_ids": dispatched}
            )
        return len(dispatched)

    async def run_forever(self) -> None:
        while True:
            await self.reconcile()
            await asyncio.sleep(self._interval)
