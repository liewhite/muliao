"""会话领域对外发布的事件与它接收的定向命令（platform.messaging）。设计见 docs/domain-design.md 4.1。"""

from __future__ import annotations

from enum import StrEnum

from muliao.conventions import Attachment
from muliao.platform.messaging import Command, DomainEvent


class Ending(StrEnum):
    """关联（委派来的工作）的结局。"""

    FINISHED = "finished"
    STOPPED = "stopped"
    FAILED = "failed"
    DELETED = "deleted"


class SessionSettled(DomainEvent):
    """会话结清了这些关联。text：完成时是会话的回答，被停止时是停止前说的话，失败时是错误说明，被删除时为空。"""

    TYPE = "session.settled"
    session_id: str
    agent_id: str
    # 结清时 agent 的展示名：交回结局的文字里用它，发布时 agent 一定存在（之后它可能被删除）
    agent_name: str
    # thread 名（委派来的工作一定在 thread 里）
    thread: str
    ending: Ending
    text: str
    # 这段工作用工具挂给用户的文件（conventions.TOOL_ATTACHMENTS）
    attachments: list[Attachment]
    correlations: list[str]


class SessionStopped(DomainEvent):
    """会话被用户停止：cleared 是被清掉的占用（它在外面的工作被放弃）。"""

    TYPE = "session.stopped"
    session_id: str
    cleared: list[str]


class DelegationDispatched(DomainEvent):
    """会话把一项要交回结局的工作投给了 thread（runtime.conversation.Threads.dispatch）。"""

    TYPE = "delegation.dispatched"
    holder: str
    owner_session_id: str
    child_session_id: str
    call_id: str | None
    awaited: bool


class PostEvent(Command):
    """往会话的信箱投一封事件信件，同时释放一个占用（后台任务的结局）。占用已被停止清掉、或会话已删除时不投。"""

    TYPE = "session.post_event"
    session_id: str
    source: str
    content: str
    awaited: bool
    key: str
    release: str
