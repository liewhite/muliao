"""会话领域对外发布的事件与它接收的定向命令（platform.messaging）。设计见 docs/architecture.md 第 7 节。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from muliao.platform.messaging import Command, DomainEvent
from muliao.sessions.origins import Sender, SessionOrigin

# 运行停在哪类挂起的调用上：等用户回答提问、等被问的 agent、等用户批准（与回答信封的 tool 同名，sessions.pending.KINDS）
type Waiting = Literal["ask_user", "ask_agent", "approval"]


class RunEnded(DomainEvent):
    """一次运行结束了（完成、停在挂起的调用上、被停止）；没有可交给模型的空闲收尾不发。只带事实，要不要推送、要不要认领
    由订阅方按自己的规则判断。"""

    TYPE = "session.run_ended"
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

    TYPE = "session.run_failed"
    session_id: str
    agent_id: str
    agent_name: str
    run_id: int
    origin: SessionOrigin
    # 失败的原文（task.error：异常类型与信息），怎么截给人看由订阅方定
    error: str


class SessionStopped(DomainEvent):
    """会话被用户停止：cleared 是被清掉的外部工作的占用者（如 job:12，它在外面的工作被放弃；委派由会话领域自己停）。"""

    TYPE = "session.stopped"
    session_id: str
    cleared: list[str]


class SessionDeleted(DomainEvent):
    """会话被删除：引用它的数据由各领域清理（如指向它的通知）。"""

    TYPE = "session.deleted"
    session_id: str
    agent_id: str


class SessionRead(DomainEvent):
    """用户看过了这个会话（打开它、看着它时又有了新回答）。会话的未读就是指向它的未读通知（通知领域），标记
    已读即把它们标为已读。"""

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
    """在会话之外进行的一项工作还在进行：给会话投一封检查信件（规则见 work.post_check_in）。发送方在确认工作仍在
    进行的事务里发出；与这项工作的 WorkEnded 同一条 lane，所以检查信件不会排在结束之后。失败后被人工重试时可能晚于
    结束执行：那时占用已释放，post_check_in 不投。"""

    TYPE = "session.work_check_in"
    session_id: str
    holder: str
    text: str


class NewConversation(BaseModel):
    """别的领域的来信要新建的顶层会话：id 由发送方预先分配（投递是异步的，发送方要立即告诉它的调用方会话 id），
    标题由发送方给出。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    to: Literal["new"] = "new"
    id: str
    agent_id: str
    title: str


class SessionKey(BaseModel):
    """按发送方定位的会话：来信的发送方（SenderMail.sender）给这个 agent 的会话，没有就以 title 新建。发送方因此不用
    记会话 id（issue：每个 issue 与 agent 对应唯一一个会话）。有多个时（同一发送方新建过多个会话）取最新的。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    to: Literal["keyed"] = "keyed"
    agent_id: str
    title: str


# 别的领域的来信投到哪个会话：新建的会话（触发器）、按发送方定位的会话（issue）
type MailTarget = Annotated[NewConversation | SessionKey, Field(discriminator="to")]


class ToSession(BaseModel):
    """收件方是这一个会话（续写的、或新建时预先分配了 id 的）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    to: Literal["session"] = "session"
    session_id: str


class ToKeyed(BaseModel):
    """收件方是按发送方定位的会话（SessionKey）：会话可能还没建出来，按 key 问才算得上还没投递的来信。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    to: Literal["keyed"] = "keyed"
    sender: Sender
    agent_id: str


# 投递之后能问「忙不忙」的句柄：形状由会话领域定，发送方原样存下、原样交回 SessionActivity.recipient_in
type Recipient = Annotated[ToSession | ToKeyed, Field(discriminator="to")]


class SenderMail(BaseModel):
    """别的领域（触发器、issue）的来信，经 inbound.receive_command_in 校验、定向命令 Receive 投递：只有文字，没有人在等
    （处理它的运行不推送）。sender 是具体的发送方（触发器、issue）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    sender: Sender
    content: str
    session: MailTarget


class Receive(Command):
    """把一封来信投进会话的信箱（inbound.receive），在会话领域自己的事务里。发送方在自己的事务里先用
    receive_command_in 校验、记下自己的状态，同一事务发出它（触发器触发、issue 有新动态）。投递时会话或 agent 已被
    删除（校验之后的变化）就不投，记日志。"""

    TYPE = "session.receive"
    mail: SenderMail
