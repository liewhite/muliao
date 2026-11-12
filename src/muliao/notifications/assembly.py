"""通知领域的装配：收件箱写下通知、经推送把它推到设备（inbox → push → webpush）怎么接只在这里，组合根只给依赖与配置
（同 sessions.assembly.SessionDomain）；交给组合根的部件见 platform.wiring.Parts。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai import FunctionToolset

from muliao.notifications.deliver import DeliverHandler
from muliao.notifications.inbox import Notifications
from muliao.notifications.push import DELIVER_TASK, PushSubscriptions
from muliao.notifications.tools import notify_toolset
from muliao.notifications.webpush import VapidKeys, WebPushDelivery
from muliao.platform.bus import EventBus
from muliao.platform.db import Database
from muliao.platform.messaging import Messaging
from muliao.platform.tasks import TaskQueue
from muliao.platform.tooling import RunScope
from muliao.platform.wiring import Parts
from muliao.platform.worker import TaskHandler


@dataclass(frozen=True)
class NotificationDomain(Parts):
    # 通知的写入与查询（通知页、未读数）
    notifications: Notifications
    # 浏览器推送订阅
    push_subscriptions: PushSubscriptions
    # 推送的公钥
    vapid: VapidKeys
    _handler: DeliverHandler

    @classmethod
    def build(
        cls, *, db: Database, queue: TaskQueue, bus: EventBus, vapid_key_path: Path, vapid_subject: str
    ) -> NotificationDomain:
        subscriptions = PushSubscriptions(db)
        vapid = VapidKeys(vapid_key_path)
        return cls(
            notifications=Notifications(db, queue, bus),
            push_subscriptions=subscriptions,
            vapid=vapid,
            _handler=DeliverHandler(db, WebPushDelivery(subscriptions, vapid, vapid_subject)),
        )

    def toolsets(self) -> Sequence[FunctionToolset[RunScope]]:
        """agent 通知用户的工具（notify_user）。"""
        return (notify_toolset(self.notifications),)

    def subscribe(self, messaging: Messaging) -> None:
        """写下通知的订阅（运行结束、运行失败）、随会话删除。"""
        self.notifications.subscribe(messaging)

    def handlers(self) -> Sequence[tuple[str, TaskHandler]]:
        """推送的任务种类与处理器。"""
        return ((DELIVER_TASK, self._handler),)
