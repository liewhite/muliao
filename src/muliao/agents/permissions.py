"""分发规则（能用什么）：工具登记、`kind:name` 通配符规则、内置角色，在这里唯一登记。

用途是防误操作、减少无关工具与条目占用上下文，不是安全隔离。

- 条目分四种（agents.kinds.Kind）：内置工具（tool，登记在 TOOLS，内容在代码里、归平台、不可改）与存在 items 表里的
  skill、MCP server、secret。名字按种类全局唯一。
- 规则：allow / deny 两组 `kind:glob`（fnmatch，如 `tool:issue_*`、`skill:relay-host`、`secret:RELAY_*`），deny 优先。
  一组规则「收录」一个条目即 Rules.allows（allow 匹配、deny 没拦）：条目页列出的收录角色、删 agent 时判断条目是否分发
  出去，都是这一个定义。
  agent 的规则 = 内置角色 base（每个 agent 隐含）+ 它的各个角色 + 它自己的 allow / deny，allow 与 deny 分别取并集。
- agent 能用的条目 = 它自己 owner 的 ∪ 规则放行的（agents.access）。
- 内置角色 base（全部非管理工具）与 admin（全部工具；admin 还能改任何条目，见 agents.access）：规则 = 代码里固定的部分
  （FIXED_RULES，新加的工具自动归入）∪ 存在 roles 表里可编辑的部分（如 base 收录的 skill）。
- 运行开始时按规则算出工具列表（agent_runtime.runner.Toolbox），调用时不再判断，工具列表在一次运行里不变。
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatchcase

from muliao.agents.kinds import Kind
from muliao.errors import InvalidInput


class Access(StrEnum):
    READ = "read"
    WRITE = "write"


@dataclass(frozen=True)
class Tool:
    access: Access
    # 管理类工具（管 agent、角色、全局记忆、条目归属）：base 没有，只经 admin 角色或显式规则给出
    admin: bool = False


@dataclass(frozen=True)
class ToolSpec:
    """一个内置工具的读模型（界面、get_agent）：名字、读 / 写、是否管理类。"""

    name: str
    access: Access
    admin: bool


def spec_of(name: str) -> ToolSpec:
    tool = TOOLS[name]
    return ToolSpec(name, tool.access, tool.admin)


R, W = Tool(Access.READ), Tool(Access.WRITE)
ADMIN_W = Tool(Access.WRITE, admin=True)

# 内置工具：唯一登记处。组合根在启动时核对它与实际注册的工具一一对应（web 只在配置了 [web] 时注册）
TOOLS: dict[str, Tool] = {
    # 自我改进、工作历史、提问
    "read_skill": R,
    "write_skill": W,
    "edit_skill": W,
    "delete_skill": W,
    "memory_edit": W,
    "memory_edit_public": ADMIN_W,
    "history_append": W,
    "history_search": R,
    "ask_user": R,
    # 文件与 shell
    "read_file": R,
    "write_file": W,
    "edit_file": W,
    "share_file": W,
    "run_shell": W,
    "job_status": R,
    "job_kill": W,
    # agent、角色与委派
    "list_agents": R,
    "get_agent": R,
    "create_agent": ADMIN_W,
    "update_agent": ADMIN_W,
    "delete_agent": ADMIN_W,
    "create_role": ADMIN_W,
    "update_role": ADMIN_W,
    "delete_role": ADMIN_W,
    "transfer_item": ADMIN_W,
    "delegate_task": W,
    "ask_agent": W,
    "list_threads": R,
    "stop_thread": W,
    "notify_user": W,
    # MCP 配置
    "mcp_list": R,
    "mcp_add": W,
    "mcp_remove": W,
    # vault
    "vault_list": R,
    "vault_set": W,
    "vault_delete": W,
    # trigger
    "trigger_kinds": R,
    "trigger_list": R,
    "trigger_create": W,
    "trigger_update": W,
    "trigger_run": W,
    "trigger_delete": W,
    # issue
    "issue_list": R,
    "issue_show": R,
    "issue_create": W,
    "issue_update": W,
    "issue_comment": W,
    # 网页（只在配置了 [web] 时注册）
    "web_search": R,
    "web_fetch": R,
}
_WEB_TOOLS = frozenset({"web_search", "web_fetch"})


def available_tools(*, web_configured: bool) -> tuple[str, ...]:
    """当前配置下实际注册的工具，按登记顺序。组合根启动时核对注册的工具就是它；界面、get_agent 与运行时算最终工具都只
    在它里面取，不会列出这次配置没有的工具。"""
    return tuple(n for n in TOOLS if web_configured or n not in _WEB_TOOLS)


def pattern_of(kind: Kind, name: str) -> str:
    """一个条目在规则里的名字：`kind:name`。"""
    return f"{kind.value}:{name}"


def kind_of(pattern: str) -> Kind:
    """规则的种类；不是 `kind:glob` 形式（kind 为 tool、skill、mcp、secret 之一，glob 非空）时 InvalidInput。"""
    kind, sep, glob = pattern.partition(":")
    if not sep or not glob or kind not in Kind.__members__.values():
        raise InvalidInput(
            f"rule {pattern!r} must look like 'kind:name' with kind one of {[k.value for k in Kind]} "
            "(fnmatch wildcards allowed in the name), e.g. 'tool:issue_*', 'skill:relay-host', 'secret:RELAY_*'"
        )
    return Kind(kind)


def check_patterns(patterns: Sequence[str]) -> None:
    """不查数据库的规则校验：形式合法、不重复，tool 规则至少匹配一个登记的工具（拼错的在写入时报出，而不是悄悄什么都不
    匹配）。skill / mcp / secret 规则是否匹配现有条目由写入方在事务里查（agents.roles.check_new_items_in）。"""
    bad = [
        p
        for p in patterns
        if kind_of(p) == Kind.TOOL and not any(fnmatchcase(pattern_of(Kind.TOOL, t), p) for t in TOOLS)
    ]
    if bad:
        raise InvalidInput(f"rules {bad} match no tool; tools are listed by get_agent, e.g. 'tool:issue_*'")
    if len(set(patterns)) != len(patterns):
        raise InvalidInput(f"rules must be distinct, got {list(patterns)}")


@dataclass(frozen=True)
class Rules:
    """一组 allow / deny 的 `kind:glob`；deny 优先。"""

    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()

    @staticmethod
    def union(rules: Iterable[Rules]) -> Rules:
        rules = list(rules)
        return Rules(
            allow=tuple(dict.fromkeys(p for r in rules for p in r.allow)),
            deny=tuple(dict.fromkeys(p for r in rules for p in r.deny)),
        )

    def allows(self, kind: Kind, name: str) -> bool:
        """这组规则收录它：allow 匹配、deny 没拦。"""
        item = pattern_of(kind, name)
        return any(fnmatchcase(item, p) for p in self.allow) and not any(
            fnmatchcase(item, p) for p in self.deny
        )

    def tools(self, available: Collection[str]) -> list[str]:
        """available（当前配置下注册的工具，available_tools）里放行的，按登记顺序。"""
        return [n for n in TOOLS if n in available and self.allows(Kind.TOOL, n)]


@dataclass(frozen=True)
class Role:
    name: str
    description: str
    # 可编辑的规则（roles 表）
    rules: Rules
    # 内置角色在代码里固定的规则（自建角色为空）
    fixed: Rules

    @property
    def builtin(self) -> bool:
        return self.name in FIXED_RULES

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

# 内置角色固定的规则：base 是全部非管理工具（写成纯 allow：deny 优先于一切 allow，写成 deny 的话 admin 也放不开）
FIXED_RULES: dict[str, Rules] = {
    BASE: Rules(allow=tuple(pattern_of(Kind.TOOL, n) for n, t in TOOLS.items() if not t.admin)),
    ADMIN: Rules(allow=("tool:*",)),
}
