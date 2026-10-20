"""通知领域接收的命令。"""

from muliao.platform.messaging import Command


class Notify(Command):
    """给用户发一条通知（并排上推送）。发送方在自己的事务里先提交了状态、又必须保证通知发出时用它（定向命令，
    docs/domain-design.md 3.4），例如触发器的前置检查从正常变为出错。"""

    TYPE = "notifications.notify"
    # 系统通知（不属于任何 agent）为 None
    agent_id: str | None
    title: str
    body: str
    # 点开通知打开的会话；系统通知为 None
    session_id: str | None
