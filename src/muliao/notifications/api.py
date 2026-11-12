"""通知领域的公开接口：通知的写入与查询（Notifications）、浏览器推送订阅（PushSubscriptions）与推送的公钥（VapidKeys）。别的领域发通知经
组合根交来的端口 NotifyIn（notifications.events，即 Notifications.notify_in）。组合根经 NotificationDomain.build 装配。"""

from muliao.notifications.assembly import NotificationDomain
from muliao.notifications.inbox import Notifications
from muliao.notifications.push import PushSubscriptions
from muliao.notifications.webpush import VapidKeys

__all__ = ["NotificationDomain", "Notifications", "PushSubscriptions", "VapidKeys"]
