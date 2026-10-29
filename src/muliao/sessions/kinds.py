"""会话的种类与每种的规则（docs/architecture.md 第 7 节「会话种类」）：会话的行为差别只在这里定义，别处读策略，不按种类分支。"""

from dataclasses import dataclass
from enum import StrEnum


class SessionKind(StrEnum):
    # 顶层会话：用户或触发器发起
    CONVERSATION = "conversation"
    # 委派（delegate_task、ask_agent）开出的 thread：独立的会话，用户也能看、能插话；用哪个工具都能续聊
    THREAD = "thread"


@dataclass(frozen=True)
class KindPolicy:
    # 由委派开出（有父会话与 thread 名）；否则是顶层会话（用户或触发器发起）
    opened_by_delegation: bool


_POLICIES = {
    SessionKind.CONVERSATION: KindPolicy(opened_by_delegation=False),
    SessionKind.THREAD: KindPolicy(opened_by_delegation=True),
}

# 顶层会话的种类（不是委派开出的）：按发送方定位会话（inbound.SessionKey）只在这些里找
TOP_LEVEL = frozenset(k for k, p in _POLICIES.items() if not p.opened_by_delegation)


def policy_of(kind: SessionKind) -> KindPolicy:
    return _POLICIES[kind]
