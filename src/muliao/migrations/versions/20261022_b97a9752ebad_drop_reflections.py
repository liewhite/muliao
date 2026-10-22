"""drop reflections

去掉每 N 步强制反思（#116）：删除表 reflections、reflection_checkpoints 与 agents.reflect_every 列，并删除
tasks 里种类为 reflection 的任务（不再有处理器；未完成的留着会挡住 lane、在启动检查里报错）。会删数据：
反思记录与这些任务行只留在升级前的备份里。model_usage 里 kind=reflection 的账目行保留不动。

降级只恢复结构：两张表为空，reflect_every 回填 30（当时的默认值），删掉的任务行不恢复。

Revision ID: b97a9752ebad
Revises: b62e70969c23
Create Date: 2026-10-22 18:06:05.995785
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b97a9752ebad"
down_revision: str | None = "b62e70969c23"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DELETE FROM tasks WHERE kind = 'reflection'")
    with op.batch_alter_table("reflections", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_reflections_agent_id"))
    op.drop_table("reflections")
    op.drop_table("reflection_checkpoints")
    with op.batch_alter_table("agents", schema=None) as batch_op:
        batch_op.drop_column("reflect_every")


def downgrade() -> None:
    with op.batch_alter_table("agents", schema=None) as batch_op:
        batch_op.add_column(sa.Column("reflect_every", sa.INTEGER(), nullable=False, server_default="30"))
    op.create_table(
        "reflection_checkpoints",
        sa.Column("agent_id", sa.VARCHAR(length=32), nullable=False),
        sa.Column("message_id", sa.INTEGER(), nullable=False),
        sa.PrimaryKeyConstraint("agent_id"),
    )
    op.create_table(
        "reflections",
        sa.Column("id", sa.INTEGER(), nullable=False),
        sa.Column("agent_id", sa.VARCHAR(length=32), nullable=False),
        sa.Column("from_message_id", sa.INTEGER(), nullable=False),
        sa.Column("to_message_id", sa.INTEGER(), nullable=False),
        sa.Column("output", sa.TEXT(), nullable=False),
        sa.Column("messages", sa.TEXT(), nullable=False),
        sa.Column("created_at", sa.DATETIME(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("reflections", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_reflections_agent_id"), ["agent_id"], unique=False)
