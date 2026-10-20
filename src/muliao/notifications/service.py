"""通知：发给用户的一条消息（标题、正文、指向的会话、是否未读）。通知页与应用角标的未读数是它的展示；投递
（Web Push）是 delivery 的事，deliver task 只引用通知，内容只存在 notifications 表。

Notifications 是这个领域的唯一服务：写下通知并排一个 deliver task（notify、定向命令 Notify、订阅 RunEnded），通知页
与未读数，随会话删除（订阅 SessionDeleted）。同一会话的两种事件在同一条 lane 上（session_events_lane）。
"""

import logging

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.notifications.events import Notify
from muliao.notifications.tables import Notification
from muliao.platform.db import Database
from muliao.platform.messaging import Delivery, Messaging
from muliao.platform.tables import TaskKind
from muliao.platform.tasks import Commit, TaskQueue, no_commit
from muliao.sessions.api import session_in
from muliao.sessions.events import RunEnded, SessionDeleted

log = logging.getLogger(__name__)


def deliver_lane(session_id: str | None, agent_id: str | None) -> str:
    """投递通知的 lane：同一会话（没有会话时同一 agent、系统通知一条）的推送按顺序。"""
    return f"deliver:{session_id or agent_id or 'system'}"


def session_events_lane(event: RunEnded | SessionDeleted) -> str:
    """同一个会话的事件在同一条 lane 上按发布顺序处理：RunEnded 一定先于 SessionDeleted 发布（会话忙时不能删除），
    删除的清理之后不会再为它写通知。顺序只对没有失败的投递成立：人工重试的 RunEnded 由处理器检查会话还在。"""
    return f"notifications:{event.session_id}"


class Notifications:
    def __init__(self, db: Database, queue: TaskQueue):
        self._db = db
        self._queue = queue

    async def enqueue_in(self, s: AsyncSession, agent_id: str | None, notification: Notification) -> None:
        s.add(notification)
        await s.flush()
        self._queue.enqueue(
            s,
            kind=TaskKind.DELIVER,
            lane=deliver_lane(notification.session_id, agent_id),
            agent_id=agent_id,
            session_id=notification.session_id,
            payload={"notification_id": notification.id},
        )

    def subscribe(self, messaging: Messaging) -> None:
        messaging.subscribe(RunEnded, "notifications.run_ended", self.on_run_ended, lane=session_events_lane)
        messaging.subscribe(
            SessionDeleted, "notifications.session_deleted", self.on_session_deleted, lane=session_events_lane
        )
        messaging.handle(Notify, "notifications.notify", self.on_notify)

    async def on_notify(self, command: Notify, _delivery: Delivery) -> Commit:
        async def commit(s: AsyncSession) -> None:
            await self.enqueue_in(
                s,
                command.agent_id,
                Notification(title=command.title, body=command.body, session_id=command.session_id),
            )

        return commit

    async def on_run_ended(self, event: RunEnded, _delivery: Delivery) -> Commit:
        """推送一次运行的回答：处理过的信件里有用户在等的，且有话要告诉用户（做完了，或停下来问用户）；用户自己停止的
        不推。"""
        if not event.awaited or event.stopped or (event.paused and not event.asked_user):
            return no_commit

        async def commit(s: AsyncSession) -> None:
            # 正常情况下同一会话的 SessionDeleted 排在后面（session_events_lane）；这条投递失败后被人工重试时可能已在
            # 删除之后。会话只会从存在变为删除，在本事务里检查没有竞态
            if await session_in(s, event.session_id) is None:
                log.info(
                    "run of a deleted session not notified",
                    extra={"session_id": event.session_id, "run_id": event.run_id},
                )
                return
            await self.enqueue_in(
                s,
                event.agent_id,
                Notification(
                    title=f"{event.agent_name} · {event.session_title}",
                    body=event.answer,
                    session_id=event.session_id,
                ),
            )

        return commit

    async def notify(self, agent_id: str | None, title: str, body: str, session_id: str | None) -> None:
        """agent_id 为 None 表示系统通知，不属于任何 agent；session_id 是点开通知打开的会话。"""
        async with self._db.transaction() as s:
            await self.enqueue_in(s, agent_id, Notification(title=title, body=body, session_id=session_id))

    async def on_session_deleted(self, event: SessionDeleted, _delivery: Delivery) -> Commit:
        """指向被删会话的通知随它删除。"""

        async def commit(s: AsyncSession) -> None:
            await s.execute(delete(Notification).where(Notification.session_id == event.session_id))

        return commit

    async def mark_session_read(self, session_id: str) -> None:
        """打开会话即看过了指向它的通知。"""
        async with self._db.transaction() as s:
            await s.execute(
                update(Notification)
                .where(Notification.session_id == session_id, Notification.unread.is_(True))
                .values(unread=False)
            )

    async def list(self, limit: int) -> list[Notification]:
        async with self._db.transaction() as s:
            query = select(Notification).order_by(Notification.id.desc()).limit(limit)
            return list((await s.scalars(query)).all())

    async def unread_count(self) -> int:
        async with self._db.transaction() as s:
            return await unread_count_in(s)

    async def mark_read(self, through_id: int) -> None:
        """标记到 through_id 为止的通知已读：之后才到的通知用户还没看到，保持未读。"""
        async with self._db.transaction() as s:
            await s.execute(
                update(Notification)
                .where(Notification.id <= through_id, Notification.unread.is_(True))
                .values(unread=False)
            )


async def unread_count_in(s: AsyncSession) -> int:
    query = select(func.count()).select_from(Notification).where(Notification.unread.is_(True))
    return (await s.execute(query)).scalar_one()
