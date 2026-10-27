"""通知领域的公开接口：通知的写入与查询（Notifications）、浏览器推送订阅（PushSubscriptions）与推送的公钥（VapidKeys）。别的领域发通知用
定向命令 Notify（notifications.events），不直接调用这里的写入。推送的任务种类是 DELIVER_TASK。"""

from muliao.notifications.inbox import Notifications
from muliao.notifications.push import DELIVER_TASK, PushSubscriptions
from muliao.notifications.webpush import VapidKeys

__all__ = ["DELIVER_TASK", "Notifications", "PushSubscriptions", "VapidKeys"]
