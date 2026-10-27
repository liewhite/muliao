"""种类的唯一定义：条目的种类、存在 items 表里的种类、有版本的对象种类，以及给人和模型看的名字。

- Kind：条目的种类，也是规则 `kind:glob` 里的 kind。内置工具（tool）登记在代码里（agents.permissions.TOOLS），归平台、
  没有内容与版本；其余三种存在 items 表里。
- StoredKind：items.kind，可以新建、改、删、转移 owner、看版本。
- Subject：revisions.subject，有版本的对象：三种条目，加上记忆（name 是 agent 的 id，全局记忆为空串）、角色（name 是
  角色名，文档是描述与可编辑的规则）、agent 的分发规则（name 是 agent 的 id，文档是角色与 allow / deny）。

三者的值相同处一一对应（tests/test_permissions.py 核对），互相转换只用这里的函数。
"""

from __future__ import annotations

from enum import StrEnum


class Kind(StrEnum):
    TOOL = "tool"
    SKILL = "skill"
    MCP = "mcp"
    SECRET = "secret"


class StoredKind(StrEnum):
    SKILL = "skill"
    MCP = "mcp"
    SECRET = "secret"


class Subject(StrEnum):
    SKILL = "skill"
    MCP = "mcp"
    SECRET = "secret"
    MEMORY = "memory"
    ROLE = "role"
    AGENT = "agent"


_LABELS: dict[str, str] = {
    "tool": "built-in tool",
    "skill": "skill",
    "mcp": "MCP server",
    "secret": "secret",
    "memory": "memory",
    "role": "role",
    "agent": "agent",
}


def label(kind: Kind | StoredKind | Subject) -> str:
    """错误信息、版本说明里的名字（同一种类处处同一个说法）。"""
    return _LABELS[kind.value]


def as_kind(kind: StoredKind) -> Kind:
    return Kind(kind.value)


def as_subject(kind: StoredKind) -> Subject:
    return Subject(kind.value)
