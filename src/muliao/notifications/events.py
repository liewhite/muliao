"""通知领域接收的命令，以及推给前端的新通知事件。"""

from typing import Literal

from pydantic import BaseModel

from muliao.platform.messaging import Command


class Notify(Command):
    """给用户发一条通知（并排上推送）。发送方在自己的事务里先提交了状态、又必须保证通知发出时用它（定向命令，
    docs/architecture.md 3.4），例如触发器的前置检查从正常变为出错、定时触发因上次会话在等待而跳过、issue 交给用户。"""

    TYPE = "notifications.notify"
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
