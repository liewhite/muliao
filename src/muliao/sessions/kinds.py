"""会话的种类与每种的规则（docs/architecture.md 第 7 节「会话种类」）：会话的行为差别只在这里定义，别处读策略，不按种类分支。"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from muliao.platform.tooling import Audience
from muliao.sessions.deferred import ASK_AGENT

# 开 thread 的工具名（工具注册时显式用它，见 sessions.delegation_tools）；ask_agent 的在 sessions.deferred
DELEGATE_TASK: Final = "delegate_task"


class SessionKind(StrEnum):
    # 顶层会话：用户或触发器发起
    CONVERSATION = "conversation"
    # delegate_task 开出的 thread：独立的会话，用户也能看、能插话
    THREAD = "thread"
    # ask_agent 开出的 thread：归发问的会话所有，回答交回那次调用
    PRIVATE_THREAD = "private_thread"


@dataclass(frozen=True)
class KindPolicy:
    # 由委派开出（有父会话与 thread 名）；否则是顶层会话（用户或触发器发起）
    opened_by_delegation: bool
    # 用户能在里面发消息；不能的就是私有的（见 private）
    user_writable: bool
    # 出现在会话列表（收件箱）里、运行结束时标未读
    listed: bool
    # 归父会话所有：随父会话删除，不能单独删除
    owned_by_parent: bool
    # 运行的回答交给发问的 agent（作为那次调用的结果），而不是用户
    answers_to_agent: bool
    # 续聊这个 thread 用的工具；顶层会话为 None
    continued_by: str | None

    @property
    def private(self) -> bool:
        """私有：只有开它的会话能继续，用户只能看（list_threads 与界面据此标出）。和「用户能发消息」是同一个事实的两面，
        只存 user_writable 一份。"""
        return not self.user_writable

    def audience(self, user_waits: bool) -> Audience:
        """这次运行的回答给谁看。"""
        if self.answers_to_agent:
            return Audience.AGENT
        return Audience.USER if user_waits else Audience.NONE


_POLICIES = {
    SessionKind.CONVERSATION: KindPolicy(
        opened_by_delegation=False,
        user_writable=True,
        listed=True,
        owned_by_parent=False,
        answers_to_agent=False,
        continued_by=None,
    ),
    SessionKind.THREAD: KindPolicy(
        opened_by_delegation=True,
        user_writable=True,
        listed=True,
        owned_by_parent=False,
        answers_to_agent=False,
        continued_by=DELEGATE_TASK,
    ),
    SessionKind.PRIVATE_THREAD: KindPolicy(
        opened_by_delegation=True,
        user_writable=False,
        listed=False,
        owned_by_parent=True,
        answers_to_agent=True,
        continued_by=ASK_AGENT,
    ),
}

# 归父会话所有的种类（删除父会话时一起删除）：查询用
OWNED_BY_PARENT = frozenset(k for k, p in _POLICIES.items() if p.owned_by_parent)
# 出现在会话列表里的种类
LISTED = frozenset(k for k, p in _POLICIES.items() if p.listed)
# 顶层会话的种类（不是委派开出的）：按发送方定位会话（inbound.SessionKey）只在这些里找
TOP_LEVEL = frozenset(k for k, p in _POLICIES.items() if not p.opened_by_delegation)


def policy_of(kind: SessionKind) -> KindPolicy:
    return _POLICIES[kind]
