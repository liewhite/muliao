"""通知的投递：DeliverHandler 由任务队列驱动，把通知（muliao.notifications.service）推送到一台设备（Pusher，现有 Web Push）。
每个 deliver task 只管一个订阅，重试只重推这一台。"""

import logging
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.notifications.service import unread_count_in
from muliao.notifications.tables import Notification, PushSubscription
from muliao.platform.db import Database
from muliao.platform.tables import Task
from muliao.platform.tasks import Commit, no_commit

log = logging.getLogger(__name__)


class Pusher(Protocol):
    """把通知推送到一个订阅（实现：notifications.webpush.WebPushDelivery）。unread 是投递时的未读通知数，用作应用角标。
    推送服务暂时不可用（连不上、5xx/429）抛 ServiceUnavailable，交给任务按长窗口重试（普通 TransientError 只有约 100 秒，
    抓不住十几分钟的断网，#208）；订阅失效由实现删除。"""

    async def send(self, subscription: PushSubscription, notification: Notification, unread: int) -> None: ...


class DeliverHandler:
    def __init__(self, db: Database, pusher: Pusher):
        self._db = db
        self._pusher = pusher

    async def failed_in(self, s: AsyncSession, task: Task) -> None:
        """失败只需可见（任务记录、日志），没有等着结果的一方。"""

    async def execute(self, task: Task) -> Commit:
        notification_id = task.payload["notification_id"]
        endpoint = task.payload["endpoint"]
        async with self._db.transaction() as s:
            notification = await s.get(Notification, notification_id)
            if notification is None:
                # 通知随它指向的会话删除（订阅 SessionDeleted），deliver task 不随之删除：还在排队的执行时到这里跳过
                log.info(
                    "notification deleted before delivery, not pushing",
                    extra={"notification_id": notification_id, "session_id": task.session_id},
                )
                return no_commit
            subscription = await s.get(PushSubscription, endpoint)
            if subscription is None:
                # 排队之后设备退订了，或者上一条通知发现它已失效并删除了它
                log.info(
                    "push subscription removed before delivery, not pushing",
                    extra={"notification_id": notification_id, "session_id": task.session_id},
                )
                return no_commit
            unread = await unread_count_in(s)
        # 推送实现自己记录真实结果（送达、失效删除、被拒）
        await self._pusher.send(subscription, notification, unread)
        return no_commit
