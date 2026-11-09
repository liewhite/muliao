"""分发规则（AI 资产的 RBAC）：工具登记、`action:kind:name` 通配符规则、内置角色，在这里唯一登记。

用途是防误操作、减少无关工具与资产占用上下文，不是安全隔离。

- AI 资产分五种（agents.kinds.Kind）：内置工具（tool，登记在 TOOLS，内容在代码里、归平台、不可改）与资源目录里的文件
  （agents.assets）：skill、MCP server、secret、记忆。名字按种类全局唯一。
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
  （FIXED_RULES，新加的工具自动归入）∪ access.toml 里可编辑的部分（如 base 收录的 skill）。
- 运行开始时按规则算出工具列表（agent_runtime.toolbox.Toolbox），调用时不再判断，工具列表在一次运行里不变。
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


class Action(StrEnum):
    """对资产的动作：权限的第一段。"""

    USE = "use"
    WRITE = "write"


@dataclass(frozen=True)
class Tool:
    access: Access
    # 管理类工具（建、删 agent，以及只有少数 agent 用的 eth_sign（#423））：base 没有，只经 admin
    # 角色或显式规则给出
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
    # 工作历史、提问
    "history_append": W,
    "history_search": R,
    # 热历史：某天的会话活动与用户说的话、一个会话的时间线（#368）
    "activity_overview": R,
    "session_read": R,
    "ask_user": R,
    # 文件与 shell
    "read_file": R,
    "write_file": W,
    "edit_file": W,
    "share_file": W,
    "run_shell": W,
    "job_status": R,
    "job_kill": W,
    # agent 与委派（改 agent 配置、角色、分发规则是改资源目录里的文件，agents.assets）
    "list_agents": R,
    "get_agent": R,
    "create_agent": ADMIN_W,
    "delete_agent": ADMIN_W,
    "delegate_task": W,
    "ask_agent": W,
    "list_threads": R,
    "stop_thread": W,
    "wake_me": W,
    "notify_user": W,
    # vault：agent 自己存用户给的 token（加密写进它的资源目录）；看与删就是 ls、rm 那个文件
    "vault_set": W,
    # 用 vault 里的以太坊私钥在进程内签名（#423）：只有管钱包的 agent 用，经角色或 allow 给
    "eth_sign": ADMIN_W,
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
    "issue_stop": W,
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
    """不查数据库的规则校验：形式合法、不重复，tool 规则至少匹配一个登记的工具（拼错的在写入时报出，而不是悄悄什么都不
    匹配）。别的种类的规则是否匹配现有资产由写入方在事务里查（agents.roles.check_new_items_in）。"""
    bad = [
        p
        for p in patterns
        if kind_of(p) == Kind.TOOL
        and not any(fnmatchcase(permission_of(Action.USE, Kind.TOOL, t), p) for t in TOOLS)
    ]
    if bad:
        raise InvalidInput(
            f"rules {bad} match no tool; tools are listed by get_agent, e.g. 'use:tool:issue_*'"
        )
    if len(set(patterns)) != len(patterns):
        raise InvalidInput(f"rules must be distinct, got {list(patterns)}")


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

    def tools(self, available: Collection[str]) -> list[str]:
        """available（当前配置下注册的工具，available_tools）里放行的，按登记顺序。"""
        return [n for n in TOOLS if n in available and self.includes(Kind.TOOL, n)]


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

# 全局记忆：所有 agent 共享的那块记忆（agents.memory），由 base 收录
GLOBAL_MEMORY = "global"

# 内置角色固定的规则：base 是全部非管理工具与全局记忆（写成纯 allow：deny 优先于一切 allow，写成 deny 的话 admin 也放
# 不开）；admin 是全部工具，并能改任何资产
FIXED_RULES: dict[str, Rules] = {
    BASE: Rules(
        allow=(
            *(permission_of(Action.USE, Kind.TOOL, n) for n, t in TOOLS.items() if not t.admin),
            permission_of(Action.USE, Kind.MEMORY, GLOBAL_MEMORY),
        )
    ),
    ADMIN: Rules(allow=("use:tool:*", "write:*")),
}
