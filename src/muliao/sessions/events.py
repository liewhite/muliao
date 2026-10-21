"""会话领域对外发布的事件与它接收的定向命令（platform.messaging）。设计见 docs/architecture.md 第 7 节。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from muliao.platform.messaging import Command, DomainEvent
from muliao.sessions.origins import Origin


class Ending(StrEnum):
    """关联（委派来的工作）的结局。"""

    FINISHED = "finished"
    STOPPED = "stopped"
    FAILED = "failed"
    DELETED = "deleted"


class RunEnded(DomainEvent):
    """一次运行结束了（完成、停在挂起的调用上、被停止）；没有可交给模型的空闲收尾不发。通知、反思各自订阅。"""

    TYPE = "session.run_ended"
    session_id: str
    session_title: str
    agent_id: str
    agent_name: str
    run_id: int
    # 给用户的回答：停在提问上时是问题列表，停在等 agent 上时为空
    answer: str
    stopped: bool
    # 停在挂起的调用上（等用户回答，或等被问的 agent）
    paused: bool
    asked_user: bool
    # 这次运行处理过的信件里有用户在等的
    awaited: bool


class SessionStopped(DomainEvent):
    """会话被用户停止：cleared 是被清掉的占用（它在外面的工作被放弃）。"""

    TYPE = "session.stopped"
    session_id: str
    cleared: list[str]


class SessionDeleted(DomainEvent):
    """会话（连同它的私有 thread）被删除：引用它的数据由各领域清理（如指向它的通知）。"""

    TYPE = "session.deleted"
    session_id: str
    agent_id: str


class SessionRead(DomainEvent):
    """用户看过了这个会话（打开它、看着它时又有了新回答）。每次标记都发，不论会话原本是否未读：指向它的通知是
    订阅者自己的事实（例如一轮失败、会话没变未读时 notify_user 写下的通知）。"""

    TYPE = "session.read"
    session_id: str


class AgentSessionsCleared(DomainEvent):
    """删除 agent 的流程里，会话领域已经删除了它的全部会话。"""

    TYPE = "session.agent_sessions_cleared"
    agent_id: str


class StopThread(Command):
    """停止一个 thread：它做的委派的发起方被停止了（会话领域给自己的定向命令，停止事务里发出，见
    sessions.delegation）。thread 已被删除时什么都不做。"""

    TYPE = "session.stop_thread"
    session_id: str


class WorkEnded(Command):
    """在会话之外进行的一项工作（后台任务）结束了：往会话的信箱投一封事件信件（来源是占用者），同时释放占用。占用
    已被停止清掉、或会话已删除时不投。按占用者幂等。与这项工作的 WorkCheckIn 在同一条 lane（work_lane）上。"""

    TYPE = "session.work_ended"
    session_id: str
    holder: str
    text: str


class WorkCheckIn(Command):
    """在会话之外进行的一项工作还在进行：给会话投一封检查信件（规则见 mailbox.post_check_in）。发送方在确认工作仍在
    进行的事务里发出；与这项工作的 WorkEnded 同一条 lane，所以检查信件不会排在结束之后。失败后被人工重试时可能晚于
    结束执行：那时占用已释放，post_check_in 不投。"""

    TYPE = "session.work_check_in"
    session_id: str
    holder: str
    text: str


class ReceiveNewSession(BaseModel):
    """Receive 要新建的会话：id 由发送方预先分配。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    agent_id: str
    title: str


class ReceiveKey(BaseModel):
    """Receive 投进按发送方定位的会话（inbound.SessionKey）：没有就以 title 新建。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    agent_id: str
    title: str


class Receive(Command):
    """把一封来信投进会话的信箱（inbound.receive），在会话领域自己的事务里。发送方在自己的事务里先用
    receive_command_in 校验、记下自己的状态，同一事务发出它（触发器到点、收到 webhook）。投递时会话或 agent 已被
    删除（校验之后的变化）就不投，记日志。"""

    TYPE = "session.receive"
    origin: Origin
    origin_ref: str | None
    content: str
    awaited: bool
    # 目标会话，三者恰好给出一个：续写的会话、新建的会话、按发送方定位的会话
    session_id: str | None
    new_session: ReceiveNewSession | None
    key: ReceiveKey | None
