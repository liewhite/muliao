"""工具权限改为角色（#172）：agents.tool_groups 改名为 roles，加 allow / deny（默认 []），新建 roles 表（用户自建角色）。

数据：原来的每个工具组都有同名的内置角色（agents.permissions.BUILTIN_ROLES），所以 tool_groups 的值原样就是 roles，
改名即可。每个 agent 能用的工具不变，唯一的例外是 public 角色比原来「总是开放」的多了读 issue（issue_list / issue_show，
用户定的）：没选 issues 组的 agent 会多这两个只读工具（上线时所有 agent 都选了 issues）。用 SQLite 原生 ALTER TABLE（RENAME COLUMN / ADD COLUMN），不重建 agents 表。

降级：roles 改回 tool_groups，admin 展开成全部 12 个组（含 web：没配置 [web] 时旧代码的启动检查会因此拒绝启动，
需手动去掉），自建角色无法表达为组，从 agent 上去掉；allow / deny 与 roles 表丢弃（丢掉 deny 等于放宽权限）。

Revision ID: cba7dcdfe011
Revises: 820a32ee48fd
Create Date: 2026-10-23 16:27:17.541151
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cba7dcdfe011"
down_revision: str | None = "820a32ee48fd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 迁移时的工具组（不引用代码里的定义：迁移要按写它时的样子执行）
_GROUPS = [
    "workspace",
    "temp_agent",
    "delegation",
    "notify",
    "agents",
    "public_skills",
    "mcp",
    "mcp_admin",
    "vault",
    "triggers",
    "issues",
    "web",
]


def upgrade() -> None:
    op.create_table(
        "roles",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("allow", sa.JSON(), nullable=False),
        sa.Column("deny", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("name"),
    )
    op.execute("ALTER TABLE agents RENAME COLUMN tool_groups TO roles")
    op.add_column("agents", sa.Column("allow", sa.JSON(), server_default=sa.text("'[]'"), nullable=False))
    op.add_column("agents", sa.Column("deny", sa.JSON(), server_default=sa.text("'[]'"), nullable=False))


def downgrade() -> None:
    conn = op.get_bind()
    for agent_id, roles in conn.execute(sa.text("SELECT id, roles FROM agents")).all():
        names = json.loads(roles)
        groups = _GROUPS if "admin" in names else [g for g in names if g in _GROUPS]
        conn.execute(
            sa.text("UPDATE agents SET roles = :g WHERE id = :id"), {"g": json.dumps(groups), "id": agent_id}
        )
    op.drop_column("agents", "deny")
    op.drop_column("agents", "allow")
    op.execute("ALTER TABLE agents RENAME COLUMN roles TO tool_groups")
    op.drop_table("roles")
