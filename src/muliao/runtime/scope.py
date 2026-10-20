from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from muliao.models import Origin


class Audience(StrEnum):
    """一次运行的回答交给谁：运行开始时由会话与它处理的信件推导一次（runtime.consumer），决定开放哪些工具（ask_user）
    与指令里怎么说。不入库。"""

    # 有用户在等：运行结束时推送
    USER = "user"
    # 私有 thread：回答作为 ask_agent 的结果交回发问方
    AGENT = "agent"
    # 没有人在等（触发器、运行中检查……）：要通知用户时 agent 自己 notify_user
    NONE = "none"


@dataclass(frozen=True)
class RunScope:
    """一次 agent 运行的上下文，作为 pydantic-ai 的 deps 传给所有工具。"""

    agent_id: str
    workspace: Path
    # 反思与临时 agent 不属于任何会话，以下三项均为 None
    session_id: str | None
    origin: Origin | None
    audience: Audience | None

    @property
    def user_waits(self) -> bool:
        """用户在等这次运行的回答：它发起的后台任务、委派的结局随之推送。"""
        return self.audience == Audience.USER
