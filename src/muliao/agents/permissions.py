"""工具权限（RBAC）：每个工具的权限 id 与读写、通配符规则、内置角色，在这里唯一登记。

用途是防误操作、减少无关工具占用上下文，不是安全隔离。只管内置工具：MCP 不在其中，agent 用自己的私有 server 与
它勾选的公共 server（AgentInfo.mcp_servers，agent_runtime.mcp）。

- 权限 id 形如 `资源:动作`（`issue:list`、`agent:delete`、`skill.public:write`）。写所有 agent 共享的资源（公共 skill、
  全局记忆、公共 secret、公共 MCP 配置）的是单独的工具，权限 id 的资源带 `.public`。
- 规则：allow / deny 两组 fnmatch 通配符，deny 优先。agent 的规则 = 它的各个角色 + 它自己的 allow/deny，各自的
  allow 与 deny 分别取并集（某个角色的 deny 也作用于别的角色给的权限）。
- 角色：内置 `admin`（全部）与 `agent`（除写全局资源外的全部）；用户自建的角色存在 roles 表（agents.roles）。
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


@dataclass(frozen=True)
class Permission:
    id: str
    access: Access


R, W = Access.READ, Access.WRITE

# 工具名 → 权限：唯一登记处。组合根在启动时核对它与实际注册的工具一一对应（web 只在配置了 [web] 时注册）
TOOL_PERMISSIONS: dict[str, Permission] = {
    # 自我改进、工作历史、提问
    "read_skill": Permission("skill:read", R),
    "write_skill": Permission("skill:write", W),
    "edit_skill": Permission("skill:edit", W),
    "delete_skill": Permission("skill:delete", W),
    "memory_edit": Permission("memory:edit", W),
    "history_append": Permission("history:append", W),
    "history_search": Permission("history:search", R),
    "ask_user": Permission("user:ask", R),
    # 文件与 shell
    "read_file": Permission("file:read", R),
    "write_file": Permission("file:write", W),
    "edit_file": Permission("file:edit", W),
    "share_file": Permission("file:share", W),
    "run_shell": Permission("shell:run", W),
    "job_status": Permission("shell.job:status", R),
    "job_kill": Permission("shell.job:kill", W),
    # agent 与委派
    "list_agents": Permission("agent:list", R),
    "get_agent": Permission("agent:get", R),
    "create_agent": Permission("agent:create", W),
    "update_agent": Permission("agent:update", W),
    "delete_agent": Permission("agent:delete", W),
    "delegate_task": Permission("thread:delegate", W),
    "ask_agent": Permission("thread:ask", W),
    "list_threads": Permission("thread:list", R),
    "stop_thread": Permission("thread:stop", W),
    "notify_user": Permission("user:notify", W),
    # 全局资源：公共 skill、全局记忆
    "write_public_skill": Permission("skill.public:write", W),
    "edit_public_skill": Permission("skill.public:edit", W),
    "delete_public_skill": Permission("skill.public:delete", W),
    "memory_edit_public": Permission("memory.public:edit", W),
    # MCP 配置
    "mcp_list": Permission("mcp.config:list", R),
    "mcp_add": Permission("mcp.config:add", W),
    "mcp_remove": Permission("mcp.config:remove", W),
    "mcp_set_enabled": Permission("mcp.config:set_enabled", W),
    "mcp_add_public": Permission("mcp.config.public:add", W),
    "mcp_remove_public": Permission("mcp.config.public:remove", W),
    "mcp_set_enabled_public": Permission("mcp.config.public:set_enabled", W),
    # vault
    "vault_list": Permission("vault:list", R),
    "vault_set": Permission("vault:set", W),
    "vault_delete": Permission("vault:delete", W),
    "vault_set_public": Permission("vault.public:set", W),
    "vault_delete_public": Permission("vault.public:delete", W),
    # trigger
    "trigger_kinds": Permission("trigger:kinds", R),
    "trigger_list": Permission("trigger:list", R),
    "trigger_create": Permission("trigger:create", W),
    "trigger_update": Permission("trigger:update", W),
    "trigger_run": Permission("trigger:run", W),
    "trigger_delete": Permission("trigger:delete", W),
    # issue
    "issue_list": Permission("issue:list", R),
    "issue_show": Permission("issue:show", R),
    "issue_create": Permission("issue:create", W),
    "issue_update": Permission("issue:update", W),
    "issue_comment": Permission("issue:comment", W),
    # 网页
    "web_search": Permission("web:search", R),
    "web_fetch": Permission("web:fetch", R),
}


def available_tools(*, web_configured: bool) -> tuple[str, ...]:
    """当前配置下实际注册的工具（web 只在配置了 [web] 时注册），按登记顺序。组合根启动时核对注册的工具就是它；
    界面、get_agent 与运行时算最终工具都只在它里面取，不会列出这次配置没有的工具。"""
    return tuple(n for n, p in TOOL_PERMISSIONS.items() if web_configured or not p.id.startswith("web:"))


def check_patterns(patterns: Sequence[str]) -> None:
    """规则里的通配符必须至少匹配一个已登记的权限：拼错的规则在写入时报出，而不是悄悄什么都不匹配。"""
    ids = [p.id for p in TOOL_PERMISSIONS.values()]
    bad = [p for p in patterns if not any(fnmatchcase(i, p) for i in ids)]
    if bad:
        raise InvalidInput(
            f"permission patterns {bad} match no tool permission; ids look like 'issue:list', "
            "'skill.public:*' (get_agent lists them all)"
        )
    if len(set(patterns)) != len(patterns):
        raise InvalidInput(f"permission patterns must be distinct, got {list(patterns)}")


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

    def tools(self, available: Collection[str]) -> list[str]:
        """available（当前配置下注册的工具，available_tools）里放行的，按登记顺序。"""
        return [n for n, perm in TOOL_PERMISSIONS.items() if n in available and self.allows(perm.id)]


@dataclass(frozen=True)
class Resolved:
    """按规则最终能用的工具（当前配置下注册的里面放行的，按登记顺序）。MCP 不在其中（不归权限管）。"""

    tools: list[str]

    @staticmethod
    def of(rules: Rules, available: Collection[str]) -> Resolved:
        return Resolved(rules.tools(available))


@dataclass(frozen=True)
class Role:
    name: str
    description: str
    rules: Rules
    # 内置角色在代码里定义，不能改、不能删
    builtin: bool


ADMIN = "admin"
AGENT = "agent"

# 写所有 agent 共享的资源（全局写）与管理 agent：agent 角色没有
GLOBAL_WRITES: tuple[str, ...] = (
    "skill.public:*",
    "memory.public:*",
    "vault.public:*",
    "mcp.config.public:*",
    "agent:create",
    "agent:update",
    "agent:delete",
)

# agent 角色写成纯 allow（从登记表里去掉全局写得出），不用 deny：各来源的 deny 取并集、优先于一切 allow，写成 deny
# 的话 agent 自己的 allow、别的角色（包括 admin）都放不开全局写
BUILTIN_ROLES: dict[str, Role] = {
    ADMIN: Role(ADMIN, "全部工具", Rules(allow=("*",)), builtin=True),
    AGENT: Role(
        AGENT,
        "除写全局资源（公共 skill、全局记忆、公共 secret、公共 MCP 配置）和管理 agent 以外的全部工具",
        Rules(
            allow=tuple(
                p.id
                for p in TOOL_PERMISSIONS.values()
                if not any(fnmatchcase(p.id, g) for g in GLOBAL_WRITES)
            )
        ),
        builtin=True,
    ),
}

# 新建 agent 未指定角色时
DEFAULT_ROLES: tuple[str, ...] = (AGENT,)
