"""agent 的分发规则拆到自己的表 agent_rules（#242）：agents 的 roles / allow / deny 三列搬进 agent_rules（每个 agent 一行，
外键随 agent 级联删除），然后删掉这三列（SQLite 原生 DROP COLUMN，不重建 agents 表：重建会经级联删掉条目与记忆）。

数据：每个 agent（含正在删除的）的三列原样搬过去，版本表不动（subject agent 的版本本来就只记这三项）。

降级：agents 加回三列（都带 server_default '[]'：SQLite 加 NOT NULL 列必须有默认值，原 roles 列没有默认值，这一点差异不影响
旧代码，它写入时总给出 roles），从 agent_rules 搬回，删表。

Revision ID: abea1558f93c
Revises: acd7fd2ea877
Create Date: 2026-10-27 13:27:04.806158
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "abea1558f93c"
down_revision: str | None = "acd7fd2ea877"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = ("roles", "allow", "deny")


def upgrade() -> None:
    op.create_table(
        "agent_rules",
        sa.Column("agent_id", sa.String(length=32), nullable=False),
        sa.Column("roles", sa.JSON(), nullable=False),
        sa.Column("allow", sa.JSON(), nullable=False),
        sa.Column("deny", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("agent_id"),
    )
    op.execute(
        "INSERT INTO agent_rules (agent_id, roles, allow, deny) SELECT id, roles, allow, deny FROM agents"
    )
    for column in _COLUMNS:
        op.drop_column("agents", column)


def downgrade() -> None:
    for column in _COLUMNS:
        op.add_column("agents", sa.Column(column, sa.JSON(), server_default=sa.text("'[]'"), nullable=False))
    op.execute(
        "UPDATE agents SET "
        + ", ".join(f"{c} = (SELECT r.{c} FROM agent_rules r WHERE r.agent_id = agents.id)" for c in _COLUMNS)
        + " WHERE id IN (SELECT agent_id FROM agent_rules)"
    )
    op.drop_table("agent_rules")
