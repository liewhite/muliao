"""通知领域的公开接口：通知的写入与查询（Notifications）、浏览器推送订阅（PushSubscriptions）。别的领域发通知用
定向命令 Notify（notifications.events），不直接调用这里的写入。"""

from muliao.notifications.service import Notifications
from muliao.notifications.webpush import PushSubscriptions

__all__ = ["Notifications", "PushSubscriptions"]
