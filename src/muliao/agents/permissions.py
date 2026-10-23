"""工具权限（RBAC）：每个工具的权限 id 与访问级别、通配符规则、内置角色，在这里唯一登记。

用途是防误操作、减少无关工具占用上下文，不是安全隔离（有 `shell:run` 就能绕开其余权限）。

- 权限 id 形如 `资源:动作`（`issue:list`、`agent:delete`、`skill.public:write`）；MCP 工具是 `mcp:<server>:<tool>`，
  按运行时可见的 server 现场得出，不在表里登记。
- 规则：allow / deny 两组 fnmatch 通配符，deny 优先。agent 的规则 = public 角色 + 它的各个角色 + 它自己的 allow/deny，
  各自的 allow 与 deny 分别取并集（某个角色的 deny 也作用于别的角色给的权限）。
- 角色：`public` 每个 agent 自动拥有，不用选；`admin` 是 `*`；另有与原工具组同名的内置角色。用户自建的角色存在
  roles 表（agents.roles）。
- 运行开始时按规则算出工具列表（agent_runtime.runner.Toolbox），调用时不再判断，工具列表在一次运行里不变。
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatchcase

from muliao.errors import InvalidInput


class Access(StrEnum):
    READ = "read"
    WRITE = "write"
    # 做了就收不回（删除不带版本的数据、对外发出、可执行任意命令）
    IRREVERSIBLE = "irreversible"


@dataclass(frozen=True)
class Permission:
    id: str
    access: Access
    # 界面与 get_agent 上的附注：这个权限实际能做到的、从 id 看不出来的事
    note: str = ""


R, W, X = Access.READ, Access.WRITE, Access.IRREVERSIBLE
_BYPASS = "可执行任意命令，能绕开其余权限"

# 工具名 → 权限：唯一登记处。组合根在启动时核对它与实际注册的工具一一对应（web 只在配置了 [web] 时注册）
TOOL_PERMISSIONS: dict[str, Permission] = {
    # 自我改进、工作历史、提问
    "read_skill": Permission("skill:read", R),
    "write_skill": Permission("skill:write", W),
    "edit_skill": Permission("skill:edit", W),
    "delete_skill": Permission("skill:delete", W),
    "memory_append": Permission("memory:append", W, "scope=public 时写全局记忆"),
    "memory_replace": Permission("memory:replace", W, "scope=public 时写全局记忆"),
    "history_append": Permission("history:append", W),
    "history_search": Permission("history:search", R),
    "ask_user": Permission("user:ask", R),
    # 文件与 shell
    "read_file": Permission("file:read", R),
    "write_file": Permission("file:write", W),
    "edit_file": Permission("file:edit", W),
    "share_file": Permission("file:share", W),
    "run_shell": Permission("shell:run", X, _BYPASS),
    "job_status": Permission("shell.job:status", R),
    "job_kill": Permission("shell.job:kill", W),
    "run_temp_agent": Permission("temp_agent:run", X, "临时 agent 带 shell，" + _BYPASS),
    # agent 与委派
    "list_agents": Permission("agent:list", R),
    "get_agent": Permission("agent:get", R),
    "create_agent": Permission("agent:create", W),
    "update_agent": Permission("agent:update", W, "可以给自己加权限"),
    "delete_agent": Permission("agent:delete", X),
    "delegate_task": Permission("thread:delegate", W, "借目标 agent 自己的权限做事"),
    "ask_agent": Permission("thread:ask", W, "借目标 agent 自己的权限做事"),
    "list_threads": Permission("thread:list", R),
    "stop_thread": Permission("thread:stop", W),
    "notify_user": Permission("user:notify", W),
    # 公共 skill
    "write_public_skill": Permission("skill.public:write", W, "描述进入所有 agent 的 skill 索引"),
    "edit_public_skill": Permission("skill.public:edit", W, "描述进入所有 agent 的 skill 索引"),
    "delete_public_skill": Permission("skill.public:delete", W),
    # MCP 配置（使用 MCP 工具是 mcp:<server>:<tool>）
    "mcp_list": Permission("mcp.config:list", R),
    "mcp_add": Permission("mcp.config:add", W, "可添加运行任意命令的 stdio server"),
    "mcp_remove": Permission("mcp.config:remove", W),
    "mcp_set_enabled": Permission("mcp.config:set_enabled", W),
    # vault
    "vault_list": Permission("vault:list", R),
    "vault_set": Permission("vault:set", W),
    "vault_delete": Permission("vault:delete", X),
    # trigger
    "trigger_kinds": Permission("trigger:kinds", R),
    "trigger_list": Permission("trigger:list", R),
    "trigger_create": Permission("trigger:create", W, "可以让任意 agent 带任意说明定时运行"),
    "trigger_update": Permission("trigger:update", W),
    "trigger_run": Permission("trigger:run", W),
    "trigger_delete": Permission("trigger:delete", X),
    # issue
    "issue_list": Permission("issue:list", R),
    "issue_show": Permission("issue:show", R),
    "issue_create": Permission("issue:create", W),
    "issue_update": Permission("issue:update", W),
    "issue_comment": Permission("issue:comment", W),
    # 网页
    "web_search": Permission("web:search", R, "不可信内容的入口，也是外发通道"),
    "web_fetch": Permission("web:fetch", R, "不可信内容的入口，也是外发通道"),
}

MCP_PREFIX = "mcp:"
MCP_NOTE = "tools of the MCP servers it can see, named <server>_<tool>; permission id mcp:<server>:<tool>"


def mcp_permission(server: str, tool: str) -> str:
    return f"{MCP_PREFIX}{server}:{tool}"


def mcp_server_prefix(server: str) -> str:
    """一个 server 的全部工具的权限 id 共同的前缀。"""
    return f"{MCP_PREFIX}{server}:"


def available_tools(*, web_configured: bool) -> tuple[str, ...]:
    """当前配置下实际注册的工具（web 只在配置了 [web] 时注册），按登记顺序。组合根启动时核对注册的工具就是它；
    界面、get_agent 与运行时算最终工具都只在它里面取，不会列出这次配置没有的工具。"""
    return tuple(n for n, p in TOOL_PERMISSIONS.items() if web_configured or not p.id.startswith("web:"))


def check_patterns(patterns: Sequence[str]) -> None:
    """规则里的通配符必须至少匹配一个已登记的权限，或是 MCP 的（MCP 工具运行时才知道）：`mcp:<server>:<tool>`，至少
    写到 server 之后的冒号，或以 * 结尾（如 `mcp:*`）。拼错的规则在写入时报出，而不是悄悄什么都不匹配。"""
    ids = [p.id for p in TOOL_PERMISSIONS.values()]

    def mcp_shaped(p: str) -> bool:
        return p.startswith(MCP_PREFIX) and (p.count(":") >= 2 or p.endswith("*"))

    bad = [p for p in patterns if not mcp_shaped(p) and not any(fnmatchcase(i, p) for i in ids)]
    if bad:
        raise InvalidInput(
            f"permission patterns {bad} match no tool permission; ids look like 'issue:list', "
            "'skill.public:*', 'mcp:<server>:<tool>' (get_agent lists them all)"
        )
    if len(set(patterns)) != len(patterns):
        raise InvalidInput(f"permission patterns must be distinct, got {list(patterns)}")


def _literal_prefix(pattern: str) -> str:
    """通配符之前的字面部分。"""
    for i, ch in enumerate(pattern):
        if ch in "*?[":
            return pattern[:i]
    return pattern


@dataclass(frozen=True)
class Rules:
    """一组 allow / deny 通配符；deny 优先。"""

    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()

    @staticmethod
    def union(rules: Iterable[Rules]) -> Rules:
        rules = list(rules)
        return Rules(
            allow=tuple(dict.fromkeys(p for r in rules for p in r.allow)),
            deny=tuple(dict.fromkeys(p for r in rules for p in r.deny)),
        )

    def allows(self, permission: str) -> bool:
        return any(fnmatchcase(permission, p) for p in self.allow) and not any(
            fnmatchcase(permission, p) for p in self.deny
        )

    def may_allow_prefix(self, prefix: str) -> bool:
        """是否可能放行某个以 prefix 开头的权限（MCP 的工具连上 server 才知道）：不可能时不连接。allow 看通配符之前的
        字面部分是否与 prefix 相容；deny 只认整段覆盖 prefix 的（`*`、`mcp:*`、`mcp:<server>:*`）。保守：可能放行就
        连接，实际工具再逐个按 allows 过滤。"""
        allowed = any(
            (lit := _literal_prefix(p)).startswith(prefix) or prefix.startswith(lit) for p in self.allow
        )
        covered = any(
            p.endswith("*") and _literal_prefix(p) == p[:-1] and prefix.startswith(p[:-1]) for p in self.deny
        )
        return allowed and not covered

    def tools(self, available: Collection[str]) -> list[str]:
        """available（当前配置下注册的工具，available_tools）里放行的，按登记顺序；不含 MCP。"""
        return [n for n, perm in TOOL_PERMISSIONS.items() if n in available and self.allows(perm.id)]


@dataclass(frozen=True)
class Resolved:
    """按规则最终能用的工具（当前配置下注册的里面放行的，按登记顺序），以及是否可能有 MCP 工具。"""

    tools: list[str]
    mcp: bool

    @staticmethod
    def of(rules: Rules, available: Collection[str]) -> Resolved:
        return Resolved(rules.tools(available), rules.may_allow_prefix(MCP_PREFIX))


@dataclass(frozen=True)
class Role:
    name: str
    description: str
    rules: Rules
    # 内置角色在代码里定义，不能改、不能删
    builtin: bool


PUBLIC = "public"
ADMIN = "admin"


def _builtin(name: str, description: str, *allow: str) -> Role:
    return Role(name, description, Rules(allow=allow), builtin=True)


# 内置角色。public 每个 agent 自动拥有；其余与原来的工具组同名（迁移时 agent 的工具组原样变成角色）
BUILTIN_ROLES: dict[str, Role] = {
    r.name: r
    for r in (
        _builtin(
            PUBLIC,
            "所有 agent 自动拥有：记忆、私有 skill、工作历史、向用户提问、读 issue",
            "skill:read",
            "skill:write",
            "skill:edit",
            "skill:delete",
            "memory:*",
            "history:*",
            "user:ask",
            "issue:list",
            "issue:show",
        ),
        _builtin(ADMIN, "全部工具", "*"),
        _builtin("workspace", "文件与 shell", "file:*", "shell:*", "shell.job:*"),
        _builtin("temp_agent", "临时 agent", "temp_agent:*"),
        _builtin("delegation", "委派给其他 agent", "agent:list", "thread:*"),
        _builtin("notify", "推送通知", "user:notify"),
        _builtin("agents", "管理 agent", "agent:get", "agent:create", "agent:update", "agent:delete"),
        _builtin("public_skills", "写公共 skill", "skill.public:*"),
        _builtin("mcp", "使用 MCP", "mcp:*"),
        _builtin("mcp_admin", "管理 MCP", "mcp.config:*"),
        _builtin("vault", "管理 vault", "vault:*"),
        _builtin("triggers", "管理触发器", "trigger:*"),
        _builtin("issues", "issue 看板", "issue:*"),
        _builtin("web", "网页搜索与抓取", "web:*"),
    )
}

# 新建 agent 未指定角色时
DEFAULT_ROLES: tuple[str, ...] = (ADMIN,)
