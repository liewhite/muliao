"""通知：发给用户的一条消息（标题、正文、指向的会话、是否未读）。通知页与应用角标的未读数是它的展示；投递
（Web Push）是 delivery 的事，deliver task 只引用通知，内容只存在 notifications 表。

- Notifier：在业务事务里写下通知并排一个 deliver task。
- Notifications：通知页与未读数。
"""

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.db import Database
from muliao.models import Notification, TaskKind
from muliao.runtime.tasks import TaskQueue, deliver_lane


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
