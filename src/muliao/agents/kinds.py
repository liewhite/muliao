"""种类的唯一定义：AI 资源的种类、存成文件的种类，以及给人和模型看的名字。

- Kind：AI 资源的种类，也是规则 `action:kind:glob` 里的 kind。内置工具（tool）在代码里、各自注册时声明属性（工具表
  agents.permissions.ToolTable），归平台、没有内容；path 是本机上的目录，不是资源；其余四种是资源目录里的文件（agents.assets）。
- StoredKind：存成文件的四种，可以新建、改、删、转移 owner。

两者的值相同处一一对应（tests/test_permissions.py 核对），互相转换只用这里的函数。
"""

from __future__ import annotations

from enum import StrEnum


class Kind(StrEnum):
    TOOL = "tool"
    SKILL = "skill"
    MCP = "mcp"
    SECRET = "secret"
    MEMORY = "memory"
    # 本机上能写的目录（#416）：只有 `write:path:<目录>` 一种规则，放开这个目录及其下的写（workspace.writable）
    PATH = "path"


class StoredKind(StrEnum):
    SKILL = "skill"
    MCP = "mcp"
    SECRET = "secret"
    MEMORY = "memory"


_LABELS: dict[str, str] = {
    "tool": "built-in tool",
    "skill": "skill",
    "mcp": "MCP server",
    "secret": "secret",
    "memory": "memory",
    "path": "writable path",
}


def label(kind: Kind | StoredKind) -> str:
    """错误信息里的名字（同一种类处处同一个说法）。"""
    return _LABELS[kind.value]


def as_kind(kind: StoredKind) -> Kind:
    return Kind(kind.value)
