"""内置角色只留 admin / agent，MCP 移出工具权限（#180）：agents 加 mcp_servers（勾选的公共 MCP server 名，默认 []）。

数据：roles 里有 admin 的 agent 改为 ["admin"]，其余改为 ["agent"]（内置角色只剩这两个；agent 角色比原来按组配置的
多数 agent 工具更多：除写全局资源与管理 agent 外都有），allow / deny 清空（上线时 allow 全空，deny 只用来去掉
agent 管理，agent 角色本就没有）。自建角色（roles 表）原样保留，引用它们的 agent 也一并改成上面两种之一（上线时没有）。
mcp_servers 一律为 []：迁移读不到资源仓库里有哪些公共 server，原来有 mcp 组的 agent 的勾选在部署后补上。
用 SQLite 原生 ALTER TABLE（ADD / DROP COLUMN），不重建 agents 表。

降级：admin 保持 ["admin"]；其余展开成除 agents、public_skills、mcp 外的旧工具组，勾选过公共 MCP 的再加 mcp，
allow 补上 "agent:get"（新的 agent 角色有 get_agent，旧组里它在 agents 组），删掉 mcp_servers。

Revision ID: e8964a50067c
Revises: cba7dcdfe011
Create Date: 2026-10-24 01:29:52.183730
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e8964a50067c"
down_revision: str | None = "cba7dcdfe011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 降级时 agent 角色展开成的旧工具组（不引用代码里的定义：迁移要按写它时的样子执行）
_AGENT_GROUPS = [
    "workspace",
    "temp_agent",
    "delegation",
    "notify",
    "mcp_admin",
    "vault",
    "triggers",
    "issues",
    "web",
]


def upgrade() -> None:
    op.add_column(
        "agents", sa.Column("mcp_servers", sa.JSON(), server_default=sa.text("'[]'"), nullable=False)
    )
    conn = op.get_bind()
    for agent_id, roles in conn.execute(sa.text("SELECT id, roles FROM agents")).all():
        role = "admin" if "admin" in json.loads(roles) else "agent"
        conn.execute(
            sa.text("UPDATE agents SET roles = :r, allow = '[]', deny = '[]' WHERE id = :id"),
            {"r": json.dumps([role]), "id": agent_id},
        )


def downgrade() -> None:
    conn = op.get_bind()
    for agent_id, roles, allow, servers in conn.execute(
        sa.text("SELECT id, roles, allow, mcp_servers FROM agents")
    ).all():
        names = json.loads(roles)
        if "admin" in names:
            continue
        groups = [*_AGENT_GROUPS, *(["mcp"] if json.loads(servers) else [])]
        expanded = [*groups, *(n for n in names if n != "agent")]
        conn.execute(
            sa.text("UPDATE agents SET roles = :r, allow = :a WHERE id = :id"),
            {
                "r": json.dumps(expanded),
                "a": json.dumps(list(dict.fromkeys([*json.loads(allow), "agent:get"]))),
                "id": agent_id,
            },
        )
    op.drop_column("agents", "mcp_servers")
