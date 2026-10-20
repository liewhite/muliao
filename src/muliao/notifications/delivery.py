"""通知的投递：DeliverHandler 由任务队列驱动，把通知（muliao.notifications.service）交给推送渠道（Pusher，现有 Web Push）。"""

import logging
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.notifications.service import unread_count_in
from muliao.notifications.tables import Notification
from muliao.platform.db import Database
from muliao.platform.tables import Task
from muliao.platform.tasks import Commit, no_commit

log = logging.getLogger(__name__)


class Pusher(Protocol):
    """把通知推送到用户的所有设备（实现：notifications.webpush.WebPushDelivery）。unread 是投递时的未读通知数，用作应用角标。"""

    async def send(self, notification: Notification, unread: int) -> None: ...


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
