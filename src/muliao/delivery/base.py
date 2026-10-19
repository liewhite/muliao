"""通知投递：Notifier 在业务事务里写 deliver task；DeliverHandler 由任务队列驱动，按 channel 分派给 Delivery 实现。"""

from dataclasses import asdict, dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.db import Database
from muliao.models import DeliveryChannel, Task, TaskKind
from muliao.runtime.tasks import Commit, TaskQueue, deliver_lane, no_commit


@dataclass(frozen=True)
class Notification:
    channel: DeliveryChannel
    # channel 内的目标：Telegram chat id；Web Push 广播给所有订阅，为 None
    target: str | None
    title: str
    body: str
    session_id: str | None


class Delivery(Protocol):
    async def send(self, notification: Notification) -> None: ...


class Notifier:
    def __init__(self, db: Database, queue: TaskQueue):
        self._db = db
        self._queue = queue

    def enqueue_in(self, s: AsyncSession, agent_name: str | None, notification: Notification) -> None:
        if notification.channel == DeliveryChannel.NONE:
            raise ValueError("cannot enqueue a notification for channel 'none'")
        self._queue.enqueue(
            s,
            kind=TaskKind.DELIVER,
            lane=deliver_lane(notification.session_id, agent_name),
            agent_name=agent_name,
            session_id=notification.session_id,
            payload=asdict(notification),
        )

    async def notify(self, agent_name: str | None, notification: Notification) -> None:
        """agent_name 为 None 表示系统通知，不属于任何 agent。"""
        async with self._db.transaction() as s:
            self.enqueue_in(s, agent_name, notification)


class DeliverHandler:
    def __init__(self, deliveries: dict[DeliveryChannel, Delivery]):
        self._deliveries = deliveries

    async def execute(self, task: Task) -> Commit:
        notification = Notification(**task.payload)
        delivery = self._deliveries.get(DeliveryChannel(notification.channel))
        if delivery is None:
            raise RuntimeError(f"delivery channel {notification.channel!r} is not configured")
        # 各 Delivery 自己记录真实结果（实际送达几台设备、是否被丢弃）
        await delivery.send(notification)

        return no_commit
