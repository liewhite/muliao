"""通知：Notifier 在业务事务里写下通知并排一个 deliver task；DeliverHandler 由任务队列驱动，把它交给 Web Push；
Notifications 是用户看到的通知页与应用角标的未读数。通知的内容只存在 notifications 表，deliver task 只引用它。"""

import logging
from typing import Protocol

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.db import Database
from muliao.models import Notification, Task, TaskKind
from muliao.runtime.tasks import Commit, TaskQueue, deliver_lane, no_commit

log = logging.getLogger(__name__)


class Pusher(Protocol):
    """把通知推送到用户的所有设备（实现：delivery.webpush.WebPushDelivery）。unread 是投递时的未读通知数，用作应用角标。"""

    async def send(self, notification: Notification, unread: int) -> None: ...


class Notifier:
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

    async def notify(self, agent_id: str | None, notification: Notification) -> None:
        """agent_id 为 None 表示系统通知，不属于任何 agent。"""
        async with self._db.transaction() as s:
            await self.enqueue_in(s, agent_id, notification)


async def unread_count_in(s: AsyncSession) -> int:
    query = select(func.count()).select_from(Notification).where(Notification.unread.is_(True))
    return (await s.execute(query)).scalar_one()


async def mark_session_read_in(s: AsyncSession, session_id: str) -> None:
    """打开会话即看过了指向它的通知。"""
    await s.execute(
        update(Notification)
        .where(Notification.session_id == session_id, Notification.unread.is_(True))
        .values(unread=False)
    )


class Notifications:
    def __init__(self, db: Database):
        self._db = db

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


class DeliverHandler:
    def __init__(self, db: Database, pusher: Pusher):
        self._db = db
        self._pusher = pusher

    async def failed_in(self, s: AsyncSession, task: Task) -> None:
        """失败只需可见（任务记录、日志），没有等着结果的一方。"""

    async def execute(self, task: Task) -> Commit:
        notification_id = task.payload["notification_id"]
        async with self._db.transaction() as s:
            notification = await s.get(Notification, notification_id)
            if notification is None:
                # 通知随会话删除，而 deliver task 已经在运行（未运行的随会话一起删除了）。用户可见的会话有运行中的
                # task 时拒绝删除，所以只有私有 thread 走到这里：它随父会话删除，删除时不检查 thread 自己的 deliver task
                log.info(
                    "notification deleted before delivery, not pushing",
                    extra={"notification_id": notification_id, "session_id": task.session_id},
                )
                return no_commit
            unread = await unread_count_in(s)
        # 推送实现自己记录真实结果（实际送达几台设备、是否被丢弃）
        await self._pusher.send(notification, unread)
        return no_commit
