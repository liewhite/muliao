"""别的领域给用户发通知的请求与端口，以及推给前端的新通知事件。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class Notify:
    """给用户发一条通知（并排上推送），在发送方自己的事务里（NotifyIn），例如触发器的前置检查告警投不到 agent 时的兜底、
    定时触发因上次会话在等待而跳过、issue 交给用户。"""

    # 系统通知（不属于任何 agent）为 None
    agent_id: str | None
    title: str
    body: str
    # 通知关于的会话（打开它即已读、随它删除）；系统通知为 None
    session_id: str | None
    # 交给用户审核的 issue：点开通知打开它（而不是会话），可以直接通过或打回；其余通知为 None
    issue_id: int | None


class NotificationEvent(BaseModel):
    """写下了一条新通知（推给前端 SSE，前端据此刷新通知页与未读数）。与有没有推送订阅无关。"""

    type: Literal["notification"] = "notification"
    notification_id: int
    session_id: str | None


# 发通知的端口（Notifications.notify_in）：issue 与通知同层、不能 import 对方的 api，由组合根交给要发通知的领域
type NotifyIn = Callable[[AsyncSession, Notify], Awaitable[None]]
