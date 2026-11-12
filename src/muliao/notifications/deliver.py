"""deliver task 的处理器：推送时从收件箱（inbox.push_message_in）取通知内容，推到一台还订阅着的设备（push.device_in）。"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.notifications.inbox import push_message_in
from muliao.notifications.push import Pusher, device_in
from muliao.platform.db import Database
from muliao.platform.tables import Task
from muliao.platform.tasks import Commit, no_commit

log = logging.getLogger(__name__)


class DeliverHandler:
    """每个 deliver task 把一条通知推到一台设备。"""

    # 推送完成后没有别的数据引用它
    prunable = True

    def __init__(self, db: Database, pusher: Pusher):
        self._db = db
        self._pusher = pusher

    async def failed_in(self, s: AsyncSession, task: Task) -> None:
        """失败只需可见（任务记录、日志），没有等着结果的一方。"""

    async def execute(self, task: Task) -> Commit:
        notification_id = task.payload["notification_id"]
        endpoint = task.payload["endpoint"]
        async with self._db.transaction() as s:
            message = await push_message_in(s, notification_id)
            if message is None:
                # 通知随它指向的会话删除（订阅 SessionDeleted），deliver task 不随之删除：还在排队的执行时到这里跳过
                log.info(
                    "notification deleted before delivery, not pushing",
                    extra={"notification_id": notification_id, "session_id": task.session_id},
                )
                return no_commit
            device = await device_in(s, endpoint)
            if device is None:
                # 排队之后设备退订了，或者上一条通知发现它已失效并删除了它
                log.info(
                    "push subscription removed before delivery, not pushing",
                    extra={"notification_id": notification_id, "session_id": task.session_id},
                )
                return no_commit
        # 推送实现自己记录真实结果（送达、失效删除、被拒）
        await self._pusher.send(device, message)
        return no_commit
