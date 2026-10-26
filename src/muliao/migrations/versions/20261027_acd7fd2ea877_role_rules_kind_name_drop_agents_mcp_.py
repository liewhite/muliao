"""分发规则改为 `kind:name`、内置角色 base / admin、删 agents.mcp_servers（#235）。

数据：
- agents.roles 去掉内置的 agent 角色（base 每个 agent 隐含，不写进 roles）；agents 与自建角色的 allow / deny 从权限 id
  （`issue:list`、`skill.public:*`）改写成匹配到的工具 `tool:<名>`（匹配全部旧工具的写作 `tool:*`；已删的 *_public 写工具
  与 mcp_set_enabled 去掉）。
- roles 加 base 一行：收录全部归平台（owner 为空）的 skill 与 secret（迁移前它们对所有 agent 可见）。admin 不加行（可编辑
  部分为空）。
- mcp_servers 的勾选改成角色：每个被勾选、仍存在的 MCP server 一个同名角色（名字被占用时加前缀 `mcp-`）收录 `mcp:<名>`，
  加给勾选了它的 agent；然后删掉 mcp_servers 列（SQLite 原生 DROP COLUMN，不重建表）。

降级：agents 的 roles 不含 admin 的补回 agent 角色；`tool:<名>` 规则改回旧权限 id（`tool:*` 为 `*`，旧表里没有的工具与
skill / mcp / secret 规则去掉）；只收录一个 `mcp:<名>` 的角色还原为 mcp_servers 勾选并删除；删掉 base、admin 的行。

Revision ID: acd7fd2ea877
Revises: f369adb6b551
Create Date: 2026-10-27 01:45:12.221001
"""

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from fnmatch import fnmatchcase

import sqlalchemy as sa
from alembic import op

revision: str = "acd7fd2ea877"
down_revision: str | None = "f369adb6b551"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 改写前的工具 → 权限 id（不引用代码里的定义：迁移要按写它时的样子执行）
_OLD_IDS = {
    "read_skill": "skill:read",
    "write_skill": "skill:write",
    "edit_skill": "skill:edit",
    "delete_skill": "skill:delete",
    "memory_edit": "memory:edit",
    "history_append": "history:append",
    "history_search": "history:search",
    "ask_user": "user:ask",
    "read_file": "file:read",
    "write_file": "file:write",
    "edit_file": "file:edit",
    "share_file": "file:share",
    "run_shell": "shell:run",
    "job_status": "shell.job:status",
    "job_kill": "shell.job:kill",
    "list_agents": "agent:list",
    "get_agent": "agent:get",
    "create_agent": "agent:create",
    "update_agent": "agent:update",
    "delete_agent": "agent:delete",
    "delegate_task": "thread:delegate",
    "ask_agent": "thread:ask",
    "list_threads": "thread:list",
    "stop_thread": "thread:stop",
    "notify_user": "user:notify",
    "write_public_skill": "skill.public:write",
    "edit_public_skill": "skill.public:edit",
    "delete_public_skill": "skill.public:delete",
    "memory_edit_public": "memory.public:edit",
    "mcp_list": "mcp.config:list",
    "mcp_add": "mcp.config:add",
    "mcp_remove": "mcp.config:remove",
    "mcp_set_enabled": "mcp.config:set_enabled",
    "mcp_add_public": "mcp.config.public:add",
    "mcp_remove_public": "mcp.config.public:remove",
    "mcp_set_enabled_public": "mcp.config.public:set_enabled",
    "vault_list": "vault:list",
    "vault_set": "vault:set",
    "vault_delete": "vault:delete",
    "vault_set_public": "vault.public:set",
    "vault_delete_public": "vault.public:delete",
    "trigger_kinds": "trigger:kinds",
    "trigger_list": "trigger:list",
    "trigger_create": "trigger:create",
    "trigger_update": "trigger:update",
    "trigger_run": "trigger:run",
    "trigger_delete": "trigger:delete",
    "issue_list": "issue:list",
    "issue_show": "issue:show",
    "issue_create": "issue:create",
    "issue_update": "issue:update",
    "issue_comment": "issue:comment",
    "web_search": "web:search",
    "web_fetch": "web:fetch",
}
# 这次删掉的工具
_REMOVED = {
    "write_public_skill",
    "edit_public_skill",
    "delete_public_skill",
    "mcp_set_enabled",
    "mcp_add_public",
    "mcp_remove_public",
    "mcp_set_enabled_public",
    "vault_set_public",
    "vault_delete_public",
}
_BASE_DESCRIPTION = "每个 agent 都有：全部非管理工具，加上这里收录的 skill、MCP server、secret"


def _now() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat(" ")


def _to_tools(patterns: list[str]) -> list[str]:
    out: list[str] = []
    for p in patterns:
        matched = [t for t, i in _OLD_IDS.items() if fnmatchcase(i, p)]
        if len(matched) == len(_OLD_IDS):
            out.append("tool:*")
            continue
        out += [f"tool:{t}" for t in matched if t not in _REMOVED]
    return list(dict.fromkeys(out))


def _to_ids(patterns: list[str]) -> list[str]:
    out: list[str] = []
    for p in patterns:
        if p == "tool:*":
            out.append("*")
        elif p.startswith("tool:"):
            out += [i for t, i in _OLD_IDS.items() if fnmatchcase(f"tool:{t}", p)]
    return list(dict.fromkeys(out))


def _check_preconditions(conn: sa.Connection) -> None:
    """换算只在这些前提下保持各 agent 能用的不变；不满足就报错（整个迁移回滚），不悄悄改变结果。"""
    problems = []
    for name, roles in conn.execute(sa.text("SELECT name, roles FROM agents")):
        if not {"agent", "admin"} & set(json.loads(roles)):
            problems.append(
                f"agent {name!r} has neither the agent nor the admin role; it would gain the base tools"
            )
    if conn.execute(sa.text("SELECT 1 FROM roles WHERE name IN ('base', 'admin')")).first():
        problems.append("a custom role is named 'base' or 'admin'")
    platform_mcp = {
        n for (n,) in conn.execute(sa.text("SELECT name FROM items WHERE kind = 'mcp' AND owner IS NULL"))
    }
    for name, selected in conn.execute(sa.text("SELECT name, mcp_servers FROM agents")):
        for server in json.loads(selected):
            row = conn.execute(
                sa.text("SELECT owner FROM items WHERE kind = 'mcp' AND name = :n"), {"n": server}
            )
            owner = row.first()
            if owner is not None and server not in platform_mcp:
                problems.append(
                    f"agent {name!r} selected MCP server {server!r}, which is now a private server"
                )
    if problems:
        raise RuntimeError("cannot convert role rules safely:\n" + "\n".join(problems))


def upgrade() -> None:
    conn = op.get_bind()
    _check_preconditions(conn)
    role_names = {"base", "admin", *(n for (n,) in conn.execute(sa.text("SELECT name FROM roles")))}
    for name, allow, deny in conn.execute(sa.text("SELECT name, allow, deny FROM roles")).all():
        conn.execute(
            sa.text("UPDATE roles SET allow = :a, deny = :d WHERE name = :n"),
            {
                "a": json.dumps(_to_tools(json.loads(allow))),
                "d": json.dumps(_to_tools(json.loads(deny))),
                "n": name,
            },
        )
    public = [
        f"{k}:{n}"
        for k, n in conn.execute(
            sa.text(
                "SELECT kind, name FROM items WHERE owner IS NULL AND kind IN ('skill', 'secret') ORDER BY kind, name"
            )
        )
    ]
    conn.execute(
        sa.text(
            "INSERT INTO roles (name, description, allow, deny, created_at, updated_at) "
            "VALUES ('base', :desc, :a, '[]', :t, :t)"
        ),
        {"desc": _BASE_DESCRIPTION, "a": json.dumps(public), "t": _now()},
    )
    servers = {
        n for (n,) in conn.execute(sa.text("SELECT name FROM items WHERE kind = 'mcp' AND owner IS NULL"))
    }
    mcp_roles: dict[str, str] = {}
    agents = conn.execute(sa.text("SELECT id, roles, allow, deny, mcp_servers FROM agents")).all()
    for _, _, _, _, selected in agents:
        for server in json.loads(selected):
            if server in servers and server not in mcp_roles:
                role = server if server not in role_names else f"mcp-{server}"
                if role in role_names:
                    raise RuntimeError(f"cannot name a role for MCP server {server!r}: {role!r} is taken")
                role_names.add(role)
                mcp_roles[server] = role
                conn.execute(
                    sa.text(
                        "INSERT INTO roles (name, description, allow, deny, created_at, updated_at) "
                        "VALUES (:n, :desc, :a, '[]', :t, :t)"
                    ),
                    {
                        "n": role,
                        "desc": f"MCP server {server}",
                        "a": json.dumps([f"mcp:{server}"]),
                        "t": _now(),
                    },
                )
    for agent_id, roles, allow, deny, selected in agents:
        new_roles = [r for r in json.loads(roles) if r != "agent"]
        new_roles += [mcp_roles[s] for s in json.loads(selected) if s in mcp_roles]
        conn.execute(
            sa.text("UPDATE agents SET roles = :r, allow = :a, deny = :d WHERE id = :id"),
            {
                "r": json.dumps(list(dict.fromkeys(new_roles))),
                "a": json.dumps(_to_tools(json.loads(allow))),
                "d": json.dumps(_to_tools(json.loads(deny))),
                "id": agent_id,
            },
        )
    op.drop_column("agents", "mcp_servers")


def downgrade() -> None:
    op.add_column(
        "agents", sa.Column("mcp_servers", sa.JSON(), server_default=sa.text("'[]'"), nullable=False)
    )
    conn = op.get_bind()
    conn.execute(sa.text("DELETE FROM roles WHERE name IN ('base', 'admin')"))
    mcp_roles: dict[str, str] = {}
    for name, allow, deny in conn.execute(sa.text("SELECT name, allow, deny FROM roles")).all():
        rules = json.loads(allow)
        if len(rules) == 1 and rules[0].startswith("mcp:") and not json.loads(deny) and "*" not in rules[0]:
            mcp_roles[name] = rules[0].removeprefix("mcp:")
            continue
        conn.execute(
            sa.text("UPDATE roles SET allow = :a, deny = :d WHERE name = :n"),
            {"a": json.dumps(_to_ids(rules)), "d": json.dumps(_to_ids(json.loads(deny))), "n": name},
        )
    for agent_id, roles, allow, deny in conn.execute(
        sa.text("SELECT id, roles, allow, deny FROM agents")
    ).all():
        names = json.loads(roles)
        kept = [r for r in names if r not in mcp_roles]
        conn.execute(
            sa.text("UPDATE agents SET roles = :r, allow = :a, deny = :d, mcp_servers = :m WHERE id = :id"),
            {
                "r": json.dumps(kept if "admin" in kept else ["agent", *kept]),
                "a": json.dumps(_to_ids(json.loads(allow))),
                "d": json.dumps(_to_ids(json.loads(deny))),
                "m": json.dumps([mcp_roles[r] for r in names if r in mcp_roles]),
                "id": agent_id,
            },
        )
    for name in mcp_roles:
        conn.execute(sa.text("DELETE FROM roles WHERE name = :n"), {"n": name})
