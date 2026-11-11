"""种类的唯一定义：AI 资源的种类、存成文件的种类，以及每种存成文件的资源的固定属性（KindSpec）。

- Kind：AI 资源的种类，也是规则 `action:kind:glob` 里的 kind。内置工具（tool）在代码里、各自注册时声明属性（工具表
  agents.permissions.ToolTable），归平台、没有内容；path 是本机上的目录，不是资源；其余四种是资源目录里的文件（agents.assets）。
- StoredKind：存成文件的四种，可以新建、改、删、转移 owner。每种的名字、放在哪、名字规则、说明在公共 skill 哪一节都在
  它的 KindSpec（spec_of），读树、写前校验、报错都只问这里。各种类文件内容的格式在各自的格式模块（memory_file、
  skill_package、mcp_config、secrets_format），读与校验按种类分派（agents.assets.load、agents.asset_files）。

两者的值相同处一一对应（tests/test_permissions.py 核对），互相转换只用这里的函数。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, get_args

from muliao.agents.naming import file_name, validate_slug
from muliao.agents.secrets_format import validate_key


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


# 讲资源目录怎么改的公共 skill（#501）的小节；报错里指向它们（agents.asset_files），小节标题（`## <名>`）与这里逐字一致，
# 改这里要同步改 skill
Section = Literal[
    "Layout", "Memory", "Skills", "MCP servers", "Secrets", "agent.toml", "Roles and permissions"
]
SECTIONS: tuple[Section, ...] = get_args(Section)


@dataclass(frozen=True)
class KindSpec:
    """一种存成文件的资源的固定属性。"""

    # 错误信息里的名字（同一种类处处同一个说法）
    label: str
    # 放在 owner 目录下哪个子目录（私有记忆例外：agent 目录里的 memory.md，agents.assets.rel_of）
    shelf: str
    # 一项是 `<名><suffix>` 的文件；skill 是 `<名>/` 目录（suffix 为空）
    suffix: str
    # 一项是目录（skill）还是一个文件
    is_directory: bool
    # 讲它的那一节
    section: Section
    # 名字合这种资源的规则，否则 InvalidInput
    check_name: Callable[[str], object]


_SPECS: dict[StoredKind, KindSpec] = {
    StoredKind.SKILL: KindSpec("skill", "skills", "", True, "Skills", lambda n: validate_slug("skill", n)),
    StoredKind.MCP: KindSpec(
        "MCP server", "mcp", ".json", False, "MCP servers", lambda n: validate_slug("MCP server", n)
    ),
    StoredKind.SECRET: KindSpec("secret", "secrets", ".toml", False, "Secrets", validate_key),
    # 平台的记忆：agent 删除或转移时挪来的私有记忆带着 agent 名（可以是中文），能当文件名就行；界面与文件工具新建的
    # 平台记忆另外要求是 slug（asset_files）
    StoredKind.MEMORY: KindSpec("memory", "memory", ".md", False, "Memory", lambda n: file_name("memory", n)),
}

_OTHER_LABELS = {Kind.TOOL: "built-in tool", Kind.PATH: "writable path"}


def spec_of(kind: StoredKind) -> KindSpec:
    return _SPECS[kind]


def label(kind: Kind | StoredKind) -> str:
    """错误信息里的名字（同一种类处处同一个说法）。"""
    if isinstance(kind, StoredKind):
        return _SPECS[kind].label
    return _OTHER_LABELS[kind] if kind in _OTHER_LABELS else _SPECS[StoredKind(kind.value)].label


def as_kind(kind: StoredKind) -> Kind:
    return Kind(kind.value)
