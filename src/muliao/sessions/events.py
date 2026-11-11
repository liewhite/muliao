"""会话领域对外发布的事件（platform.messaging）。设计见 docs/architecture.md 第 7 节。"""

from __future__ import annotations

from typing import Literal

from muliao.platform.messaging import DomainEvent
from muliao.sessions.origins import SessionOrigin

# 运行停在哪类挂起的调用上：等用户回答提问、等被问的 agent、等用户批准（与回答信封的 tool 同名，sessions.pending.KINDS）
type Waiting = Literal["ask_user", "ask_agent", "approval"]


class RunEnded(DomainEvent):
    """一次运行结束了（完成、停在挂起的调用上、被停止）；没有可交给模型的空闲收尾不发。只带事实，要不要推送、要不要认领
    由订阅方按自己的规则判断。"""

    session_id: str
    session_title: str
    origin: SessionOrigin
    agent_id: str
    agent_name: str
    run_id: int
    # 模型的最后回答；停在提问、等批准上时是问题与等批准的调用，只停在等 agent 上时为空
    answer: str
    stopped: bool
    # 停在哪几类挂起的调用上（不重复）；空是没有停在挂起的调用上
    waiting_on: list[Waiting]
    # 这次运行处理过的信件里有用户在等的
    awaited: bool


class RunFailed(DomainEvent):
    """一次运行最终失败了（暂时性失败的重试用完，或契约外的失败），在队列记下失败的事务里发布（RunHandler.failed_in）。
    不自动重试。"""

    session_id: str
    agent_id: str
    agent_name: str
    run_id: int
    origin: SessionOrigin
    # 失败的原文（task.error：异常类型与信息），怎么截给人看由订阅方定
    error: str


class SessionStopped(DomainEvent):
    """会话被用户停止：cleared 是被清掉的外部工作的占用者（如 job:12，它在外面的工作被放弃；委派由会话领域自己停）。"""

    session_id: str
    cleared: list[str]


class SessionDeleted(DomainEvent):
    """会话被删除：引用它的数据由各领域清理（如指向它的通知）。"""

    session_id: str
    agent_id: str


class SessionRead(DomainEvent):
    """用户看过了这个会话（打开它、看着它时又有了新回答）。会话的未读就是指向它的未读通知（通知领域），标记
    已读即把它们标为已读。"""

    session_id: str
