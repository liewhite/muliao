"""分发规则（AI 资产的 RBAC）：`action:kind:name` 通配符规则、内置角色与内置工具表，在这里唯一登记。

用途是防误操作、减少无关工具与资产占用上下文，不是安全隔离。

- AI 资产分五种（agents.kinds.Kind）：内置工具（tool，内容在代码里、归平台、不可改；各工具注册时自己声明读写与是否管理类，
  platform.tooling.declared，组合根收集成 ToolTable）与资源目录里的文件（agents.assets）：skill、MCP server、secret、记忆。
  名字按种类全局唯一。
- 权限 = 动作 + 资产：`use:skill:relay-host`、`write:memory:trading`。动作只有 use（用：skill 进索引、MCP 连上、secret 能
  解析、记忆放进 instructions、工具开放）与 write（改、删），二者互相独立；tool 只有 use。
- 规则：allow / deny 两组 `action:kind:glob`，整串 fnmatch（如 `use:tool:issue_*`、`*:memory:trading`、`write:*`），deny
  优先。Rules.allows 是全系统唯一的判断；一组规则「收录」一个资产即它放行 use：资源页列出的收录角色、删 agent 时判断资产
  是否分发出去，都是这一个定义。
  agent 的规则 = 内置角色 base（每个 agent 隐含）+ 它的各个角色 + 它自己的 allow / deny，allow 与 deny 分别取并集。
- agent 能对资产做某个动作 = 它是 owner（隐含的专属角色：use 与 write）或规则放行（agents.access）；deny 只拦规则分来的。
- 本机可写目录（path，#416）：`write:path:<目录>` 放开这个目录及其下的写，写在 deny 里则收回（Rules.paths，由
  workspace.writable 并进默认可写范围）；字面目录、不带通配符，不经 fnmatch，`write:*` 不含路径。
- 内置角色 base（全部非管理工具与全局记忆）与 admin（全部工具，并能改任何资产）：规则 = 代码里固定的部分
  （fixed_rules，按工具表算，新加的工具自动归入）∪ access.toml 里可编辑的部分（如 base 收录的 skill）。
- 运行开始时按规则算出工具列表（agent_runtime.toolbox.Toolbox），调用时不再判断，工具列表在一次运行里不变。
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatchcase

from muliao.agents.kinds import Kind
from muliao.errors import InvalidInput
from muliao.platform.tooling import Access


class Action(StrEnum):
    """对资产的动作：权限的第一段。"""

    USE = "use"
    WRITE = "write"


@dataclass(frozen=True)
class ToolSpec:
    """一个内置工具在分发里的样子（工具自己声明的 platform.tooling.ToolTraits 里与分发有关的部分）：名字、读 / 写、
    是否管理类（base 不含，只经 admin 角色或显式规则给出）。"""

    name: str
    access: Access
    admin: bool


class ToolTable:
    """当前配置下注册的内置工具，按注册顺序：界面、get_agent 与运行时能用的工具都只在它里面取。

    资源树（内置角色 base 的固定规则、规则引用的工具）、分发与资源池要用它，而工具集又要用它们构造（vault_toolset
    用资源树、agents_toolset 用分发）：组合根先构造它、装好工具集后由 agent_runtime.Toolbox 收集各工具的声明、bind 一次。
    bind 之前用到是组合根的 bug，当场报错（装配是同步的，快照在装配完成后才会读）。"""

    def __init__(self) -> None:
        self._specs: tuple[ToolSpec, ...] | None = None

    @classmethod
    def of(cls, specs: Iterable[ToolSpec]) -> ToolTable:
        table = cls()
        table.bind(specs)
        return table

    def bind(self, specs: Iterable[ToolSpec]) -> None:
        if self._specs is not None:
            raise RuntimeError("the tool table is already bound")
        self._specs = tuple(specs)

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        if self._specs is None:
            raise RuntimeError("the tool table is used before the composition root bound it")
        return self._specs

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.specs)


def pattern_of(kind: Kind, name: str) -> str:
    """一个资产的名字：`kind:name`（界面、get_agent 列出能用的资产）。"""
    return f"{kind.value}:{name}"


def permission_of(action: Action, kind: Kind, name: str) -> str:
    """一个权限：`action:kind:name`，规则按整串 fnmatch 匹配它。"""
    return f"{action.value}:{pattern_of(kind, name)}"


_ACTIONS = (*(a.value for a in Action), "*")


def kind_of(pattern: str) -> Kind | None:
    """规则针对的种类；`write:*` 这类不限种类的为 None。不是 `action:kind:glob`（action 为 use、write 或 *，kind 为
    Kind 之一，glob 非空）或 `action:*` 形式时 InvalidInput；tool 只有 use。"""
    action, _, rest = pattern.partition(":")
    kind, sep, glob = rest.partition(":")
    if action not in _ACTIONS or not (rest == "*" or (sep and glob and kind in Kind.__members__.values())):
        raise InvalidInput(
            f"rule {pattern!r} must look like 'action:kind:name' with action one of {list(_ACTIONS)} and kind one "
            f"of {[k.value for k in Kind]} (fnmatch wildcards allowed in the name), or 'action:*'; e.g. "
            "'use:tool:issue_*', 'use:skill:relay-host', '*:memory:trading', 'write:*'"
        )
    if rest == "*":
        return None
    if kind == Kind.TOOL and action == Action.WRITE:
        raise InvalidInput(f"rule {pattern!r}: built-in tools can only be used ('use:tool:...')")
    if kind == Kind.PATH and (
        action != Action.WRITE or not glob.startswith(("/", "~/")) or any(c in glob for c in "*?[")
    ):
        raise InvalidInput(
            f"rule {pattern!r}: a writable path rule is 'write:path:<directory>' with an absolute or '~/' path "
            "and no wildcards, e.g. 'write:path:~/projects/arb'; it lets the agent write that directory and "
            "everything under it"
        )
    return Kind(kind)


def check_patterns(patterns: Sequence[str]) -> None:
    """规则的形式校验：每条形式合法（kind_of）、不重复。是否匹配现有的工具与资产由写入方对着快照只查新加的
    （agents.distribution.check_rules），读树时由 access_file 记成问题（matches_no_tool）。"""
    for p in patterns:
        kind_of(p)
    if len(set(patterns)) != len(patterns):
        raise InvalidInput(f"rules must be distinct, got {list(patterns)}")


def matches_no_tool(pattern: str, tools: ToolTable) -> bool:
    """一条 tool 规则匹配不上 tools 里的任何工具（拼错、工具改了名，或这次配置没注册它，如没配置 [web] 时的 web_*）。"""
    return kind_of(pattern) == Kind.TOOL and not any(
        fnmatchcase(permission_of(Action.USE, Kind.TOOL, t), pattern) for t in tools.names
    )


@dataclass(frozen=True)
class Rules:
    """一组 allow / deny 的 `action:kind:glob`；deny 优先。"""

    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()

    @staticmethod
    def union(rules: Iterable[Rules]) -> Rules:
        rules = list(rules)
        return Rules(
            allow=tuple(dict.fromkeys(p for r in rules for p in r.allow)),
            deny=tuple(dict.fromkeys(p for r in rules for p in r.deny)),
        )

    def allows(self, action: Action, kind: Kind, name: str) -> bool:
        """这组规则放行这个权限：allow 匹配、deny 没拦。全系统唯一的判断。"""
        permission = permission_of(action, kind, name)
        return any(fnmatchcase(permission, p) for p in self.allow) and not any(
            fnmatchcase(permission, p) for p in self.deny
        )

    def includes(self, kind: Kind, name: str) -> bool:
        """收录：放行 use。"""
        return self.allows(Action.USE, kind, name)

    def paths(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """`write:path:<目录>` 规则里的目录：(allow 的, deny 的)，按规则顺序。路径规则是字面的目录（kind_of 不让写通配符），
        放开的是目录及其下的一切，不经 fnmatch：不限种类的 `write:*`（admin）不放开任何路径。"""
        prefix = f"{Action.WRITE.value}:{Kind.PATH.value}:"
        return (
            tuple(p.removeprefix(prefix) for p in self.allow if p.startswith(prefix)),
            tuple(p.removeprefix(prefix) for p in self.deny if p.startswith(prefix)),
        )

    def tools(self, available: Iterable[str]) -> list[str]:
        """available（内置工具的名字）里放行的，按 available 的顺序。"""
        return [n for n in available if self.includes(Kind.TOOL, n)]


@dataclass(frozen=True)
class Role:
    name: str
    description: str
    # 可编辑的规则（access.toml 的 [roles.<名>]）
    rules: Rules
    # 内置角色在代码里固定的规则（自建角色为空）
    fixed: Rules

    @property
    def builtin(self) -> bool:
        return self.name in BUILTIN_ROLES

    @property
    def assignable(self) -> bool:
        """能写进 agent 的 roles：base 每个 agent 隐含，不能写。"""
        return self.name != BASE

    def held_by(self, roles: Collection[str]) -> bool:
        """roles 是一个 agent 的 roles 字段：base 每个 agent 都有（隐含），别的要写在里面。"""
        return not self.assignable or self.name in roles

    @property
    def effective(self) -> Rules:
        return Rules.union([self.fixed, self.rules])


BASE = "base"
ADMIN = "admin"

# 全局记忆：所有 agent 共享的那块记忆（agents.memory），由 base 收录
GLOBAL_MEMORY = "global"

# 内置角色，按定义顺序
BUILTIN_ROLES = (BASE, ADMIN)


def fixed_rules(tools: ToolTable) -> dict[str, Rules]:
    """内置角色固定的规则（顺序同 BUILTIN_ROLES）：base 是 tools 里全部非管理工具与全局记忆（写成纯 allow：deny 优先于
    一切 allow，写成 deny 的话 admin 也放不开）；admin 是全部工具，并能改任何资产。"""
    return {
        BASE: Rules(
            allow=(
                *(permission_of(Action.USE, Kind.TOOL, t.name) for t in tools.specs if not t.admin),
                permission_of(Action.USE, Kind.MEMORY, GLOBAL_MEMORY),
            )
        ),
        ADMIN: Rules(allow=("use:tool:*", "write:*")),
    }


@dataclass(frozen=True)
class AgentRules:
    """agent 的分发规则：角色（不含每个 agent 隐含的 base）与它自己的 allow / deny（`action:kind:glob`）。"""

    roles: tuple[str, ...] = ()
    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()

    @classmethod
    def of(cls, roles: Sequence[str], allow: Sequence[str], deny: Sequence[str]) -> AgentRules:
        return cls(tuple(roles), tuple(allow), tuple(deny))

    @property
    def own(self) -> Rules:
        """它自己的 allow / deny（不含角色）。"""
        return Rules(self.allow, self.deny)

    def effective(self, known: Mapping[str, Role]) -> Rules:
        """最终规则：各来源（sources）的并集；known 是全部角色。"""
        return Rules.union(r for _, r in self.sources(known))

    def sources(self, known: Mapping[str, Role]) -> list[tuple[str | None, Rules]]:
        """最终规则的各个来源：(角色名, 它的规则)——base 在前、再按它的 roles——最后是 (None, 它自己的 allow / deny)。
        界面按来源解释（access.Provenance）用同一份。不存在的角色读树时已跳过（agents.access_file），这里再遇到是代码错误。"""
        missing = [r for r in self.roles if r not in known]
        if missing:
            raise RuntimeError(f"agent references unknown roles {missing}")
        return [*((r, known[r].effective) for r in (BASE, *self.roles)), (None, self.own)]
