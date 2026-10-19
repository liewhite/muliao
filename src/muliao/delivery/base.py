"""通知投递：Notifier 在业务事务里写 deliver task；DeliverHandler 由任务队列驱动，把通知交给 Web Push。"""

from dataclasses import asdict, dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.db import Database
from muliao.models import Task, TaskKind
from muliao.runtime.tasks import Commit, TaskQueue, deliver_lane, no_commit


@dataclass(frozen=True)
class Notification:
    title: str
    body: str
    # 点击通知打开的会话；系统通知为 None
    session_id: str | None


class Pusher(Protocol):
    """把通知推送到用户的所有设备（实现：delivery.webpush.WebPushDelivery）。"""

    async def send(self, notification: Notification) -> None: ...


class Notifier:
    def __init__(self, db: Database, queue: TaskQueue):
        self._db = db
        self._queue = queue

    def enqueue_in(self, s: AsyncSession, agent_id: str | None, notification: Notification) -> None:
        self._queue.enqueue(
            s,
            kind=TaskKind.DELIVER,
            lane=deliver_lane(notification.session_id, agent_id),
            agent_id=agent_id,
            session_id=notification.session_id,
            payload=asdict(notification),
        )

    async def notify(self, agent_id: str | None, notification: Notification) -> None:
        """agent_id 为 None 表示系统通知，不属于任何 agent。"""
        async with self._db.transaction() as s:
            self.enqueue_in(s, agent_id, notification)


class DeliverHandler:
    def __init__(self, pusher: Pusher):
        self._pusher = pusher

    async def execute(self, task: Task) -> Commit:
        # 推送实现自己记录真实结果（实际送达几台设备、是否被丢弃）
        await self._pusher.send(Notification(**task.payload))
        return no_commit
