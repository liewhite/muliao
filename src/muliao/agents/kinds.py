"""种类的唯一定义：AI 资源的种类、存成文件的种类，以及每种存成文件的资源的固定属性（KindSpec）。

- Kind：AI 资源的种类，也是规则 `action:kind:glob` 里的 kind。内置工具（tool）在代码里、各自注册时声明属性（工具表
  agents.permissions.ToolTable），归平台、没有内容；path 是本机上的目录，不是资源；其余四种是资源目录里的文件（agents.assets）。
- StoredKind：存成文件的四种，可以新建、改、删、转移 owner（记忆不转移，agents.catalog）。每种的名字、放在哪、名字规则、说明在公共 skill 哪一节，以及
  它的文件怎么读（load）、写之前怎么校验（vet）都在它的 KindSpec（spec_of）：后两项由各种类的格式模块（memory_file、
  skill_file、mcp_config、secrets_format）提供、在这里登记，读树（agents.assets）与写入口（agents.asset_files）只按
  登记分派，不列举种类。

两者的值相同处一一对应（tests/test_permissions.py 核对），互相转换只用这里的函数。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, get_args

from muliao.agents import mcp_config, memory_file, secrets_format, skill_file
from muliao.agents.formats import Draft, Loaded
from muliao.agents.mcp_config import McpFile
from muliao.agents.memory_file import MemoryText
from muliao.agents.naming import validate_slug
from muliao.agents.secrets_format import SecretFile, validate_key
from muliao.agents.skill_file import SkillDir


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

# 各种类专有的内容（load 读出的）；每种都有 description（记忆、MCP 为空）
Content = MemoryText | SkillDir | McpFile | SecretFile


@dataclass(frozen=True)
class KindSpec:
    """一种存成文件的资源的固定属性。"""

    # 错误信息里的名字（同一种类处处同一个说法）
    label: str
    # 放在 owner 目录下哪个子目录（私有记忆不在这里：agent 目录里的 memory.md，agents.assets.rel_of）
    shelf: str
    # 一项是 `<名><suffix>` 的文件；skill 是 `<名>/` 目录（suffix 为空）
    suffix: str
    # 一项是目录（skill）还是一个文件
    is_directory: bool
    # 讲它的那一节
    section: Section
    # 名字的规则：(label, 名字)，不合时 InvalidInput（错误信息里用 label）；用 check_name 调
    name_rule: Callable[[str, str], object]
    # 读一项：(文件或目录, 名字, 记忆的长度上限)；不合格式 InvalidInput（读树记成问题）
    load: Callable[[Path, str, int], Loaded[Content]]
    # 写之前：要写成的全文 → 实际存下的全文；不合格式 InvalidInput（写入口拒绝）
    vet: Callable[[Draft], str]

    def check_name(self, name: str) -> None:
        """名字合这种资源的规则，否则 InvalidInput。"""
        self.name_rule(self.label, name)


_SPECS: dict[StoredKind, KindSpec] = {
    StoredKind.SKILL: KindSpec(
        "skill", "skills", "", True, "Skills", validate_slug, skill_file.load, skill_file.vet
    ),
    StoredKind.MCP: KindSpec(
        "MCP server", "mcp", ".json", False, "MCP servers", validate_slug, mcp_config.load, mcp_config.vet
    ),
    StoredKind.SECRET: KindSpec(
        "secret",
        "secrets",
        ".toml",
        False,
        "Secrets",
        lambda _label, key: validate_key(key),
        secrets_format.load,
        secrets_format.vet,
    ),
    # 平台的记忆（全局与共享）；私有记忆按所属 agent 认、没有名字规则（agents.assets.rel_of）
    StoredKind.MEMORY: KindSpec(
        "memory", "memory", ".md", False, "Memory", validate_slug, memory_file.load, memory_file.vet
    ),
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
