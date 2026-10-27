"""推送：把收件箱（notifications.inbox）里的通知推到用户的设备。设备是浏览器推送订阅（push_subscriptions，只在这里
读写）；每条通知给每台设备排一个 deliver task（schedule_in），由任务队列驱动 DeliverHandler 推送，重试只重推这一台。

推送不碰收件箱的表：推送内容由收件箱给出（组合根注入的 MessageSource，返回 PushMessage）；推送实现（Pusher，
现有 Web Push）只收值对象 Device 与 PushMessage，不收 ORM 对象。"""

import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.errors import InvalidInput
from muliao.notifications.tables import PushSubscription
from muliao.platform.db import Database
from muliao.platform.tables import Task
from muliao.platform.tasks import Commit, TaskQueue, no_commit

log = logging.getLogger(__name__)

# 推送一条通知到一台设备的任务种类（组合根为它登记 DeliverHandler）
DELIVER_TASK = "deliver"


@dataclass(frozen=True)
class Device:
    """一台设备的推送订阅。endpoint 的完整路径即订阅凭据，不进日志。"""

    endpoint: str
    p256dh: str
    auth: str


@dataclass(frozen=True)
class PushMessage:
    """推到设备上的一条通知（收件箱在投递时给出）。"""

    notification_id: int
    title: str
    body: str
    # 点开打开的页面；系统通知为 None（推送点开看通知页）
    url: str | None
    session_id: str | None
    # 交给用户审核的 issue：有它时 Service Worker 给通知加「通过」「打回」按钮
    issue_id: int | None
    # 投递时的未读通知数，用作应用角标
    unread: int


# 推送时在这个事务里读通知内容（实现：notifications.inbox.push_message_in）；通知已被删除时为 None
MessageSource = Callable[[AsyncSession, int], Awaitable[PushMessage | None]]


class Pusher(Protocol):
    """把通知推送到一台设备（实现：notifications.webpush.WebPushDelivery）。推送服务暂时不可用（连不上、5xx/429）抛
    ServiceUnavailable，交给任务按长窗口重试（普通 TransientError 只有约 100 秒，抓不住十几分钟的断网，#208）；订阅
    失效由实现删除（PushSubscriptions.remove）。"""

    async def send(self, device: Device, message: PushMessage) -> None: ...


def deliver_lane(session_id: str | None, agent_id: str | None, endpoint: str) -> str:
    """投递通知的 lane：同一会话（没有会话时同一 agent、系统通知一条）推给同一台设备的按顺序；设备之间互不阻塞，
    一台在等重试不耽误别的设备。设备用 endpoint 的摘要表示：lane 会进日志，endpoint 本身是订阅凭据。"""
    device = hashlib.sha256(endpoint.encode()).hexdigest()[:12]
    return f"deliver:{session_id or agent_id or 'system'}:{device}"


class PushSubscriptions:
    """用户在界面上开启、关闭推送（订阅设备）；推送失效时由推送实现删除。"""

    def __init__(self, db: Database):
        self._db = db

    async def add(self, endpoint: str, p256dh: str, auth: str) -> None:
        if not endpoint.startswith("https://"):
            raise InvalidInput("push endpoint must be an https URL")
        async with self._db.transaction() as s:
            row = await s.get(PushSubscription, endpoint)
            if row is None:
                s.add(PushSubscription(endpoint=endpoint, p256dh=p256dh, auth=auth))
            else:
                row.p256dh, row.auth = p256dh, auth

    async def remove(self, endpoint: str) -> None:
        async with self._db.transaction() as s:
            row = await s.get(PushSubscription, endpoint)
            if row is not None:
                await s.delete(row)


async def device_in(s: AsyncSession, endpoint: str) -> Device | None:
    """还订阅着的设备；已退订或已失效删除时为 None。"""
    row = await s.get(PushSubscription, endpoint)
    return None if row is None else Device(endpoint=row.endpoint, p256dh=row.p256dh, auth=row.auth)


async def schedule_in(
    s: AsyncSession, queue: TaskQueue, notification_id: int, session_id: str | None, agent_id: str | None
) -> None:
    """在调用方（收件箱写下通知）的事务里给每台设备排一个 deliver task：一台设备暂时失败只重试它自己，不会再推给
    已经送达的设备。"""
    endpoints = (await s.scalars(select(PushSubscription.endpoint))).all()
    if not endpoints:
        log.info(
            "no push subscriptions, notification not pushed",
            extra={"notification_id": notification_id, "session_id": session_id},
        )
    for endpoint in endpoints:
        queue.enqueue(
            s,
            kind=DELIVER_TASK,
            lane=deliver_lane(session_id, agent_id, endpoint),
            agent_id=agent_id,
            session_id=session_id,
            payload={"notification_id": notification_id, "endpoint": endpoint},
        )


class DeliverHandler:
    """每个 deliver task 把一条通知推到一台设备。"""

    # 推送完成后没有别的数据引用它
    prunable = True

    def __init__(self, db: Database, message_in: MessageSource, pusher: Pusher):
        self._db = db
        self._message_in = message_in
        self._pusher = pusher

    async def failed_in(self, s: AsyncSession, task: Task) -> None:
        """失败只需可见（任务记录、日志），没有等着结果的一方。"""

    async def execute(self, task: Task) -> Commit:
        notification_id = task.payload["notification_id"]
        endpoint = task.payload["endpoint"]
        async with self._db.transaction() as s:
            message = await self._message_in(s, notification_id)
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
