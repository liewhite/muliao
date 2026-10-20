"""会话的种类与每种的规则（docs/domain-design.md 8.1）：会话的行为差别只在这里定义，别处读策略，不按种类分支。"""

from dataclasses import dataclass
from enum import StrEnum

from muliao.platform.tooling import Audience


class SessionKind(StrEnum):
    # 顶层会话：用户或触发器发起
    CONVERSATION = "conversation"
    # delegate_task 开出的 thread：独立的会话，用户也能看、能插话
    THREAD = "thread"
    # ask_agent 开出的 thread：归发问的会话所有，回答交回那次调用
    PRIVATE_THREAD = "private_thread"


@dataclass(frozen=True)
class KindPolicy:
    # 用户能在里面发消息
    user_writable: bool
    # 出现在会话列表（收件箱）里、运行结束时标未读
    listed: bool
    # 归父会话所有：随父会话删除，不能单独删除
    owned_by_parent: bool
    # 运行的回答交给发问的 agent（作为那次调用的结果），而不是用户
    answers_to_agent: bool
    # 续聊这个 thread 用的工具；顶层会话为 None
    continued_by: str | None

    def audience(self, user_waits: bool) -> Audience:
        """这次运行的回答给谁看。"""
        if self.answers_to_agent:
            return Audience.AGENT
        return Audience.USER if user_waits else Audience.NONE


_POLICIES = {
    SessionKind.CONVERSATION: KindPolicy(
        user_writable=True, listed=True, owned_by_parent=False, answers_to_agent=False, continued_by=None
    ),
    SessionKind.THREAD: KindPolicy(
        user_writable=True,
        listed=True,
        owned_by_parent=False,
        answers_to_agent=False,
        continued_by="delegate_task",
    ),
    SessionKind.PRIVATE_THREAD: KindPolicy(
        user_writable=False,
        listed=False,
        owned_by_parent=True,
        answers_to_agent=True,
        continued_by="ask_agent",
    ),
}

# 归父会话所有的种类（删除父会话时一起删除）：查询用
OWNED_BY_PARENT = frozenset(k for k, p in _POLICIES.items() if p.owned_by_parent)
# 出现在会话列表里的种类
LISTED = frozenset(k for k, p in _POLICIES.items() if p.listed)


def policy_of(kind: SessionKind) -> KindPolicy:
    return _POLICIES[kind]
