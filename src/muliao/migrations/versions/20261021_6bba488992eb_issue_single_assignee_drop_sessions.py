"""issue single assignee, drop sessions

被指派人合成一列 issues.assignee（agent id、"user" 或 NULL），由 assignee_agent_id / assigned_to_user 换算；删除
issues.sessions（issue 会话归会话领域，按 origin=issue、origin_ref 定位，不需要迁移数据）；删除时间线上已不存在的
claimed / released 两种动态（认领与让出的概念已删除）。

Revision ID: 6bba488992eb
Revises: 663aa18d63fb
Create Date: 2026-10-21 17:04:56.756452
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import sqlite

revision: str = "6bba488992eb"
down_revision: str | None = "663aa18d63fb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _keep_sequence(alter) -> None:  # noqa: ANN001
    """batch 重建表会把 sqlite_sequence 重置为现存的最大 id：记下原值并恢复，删掉的最大编号也不复用。"""
    seq = op.get_bind().exec_driver_sql("SELECT seq FROM sqlite_sequence WHERE name = 'issues'").scalar()
    alter()
    if seq is not None:
        op.execute(f"UPDATE sqlite_sequence SET seq = MAX(seq, {int(seq)}) WHERE name = 'issues'")


def upgrade() -> None:
    with op.batch_alter_table("issues", schema=None) as batch_op:
        batch_op.add_column(sa.Column("assignee", sa.String(length=32), nullable=True))
    op.execute(
        "UPDATE issues SET assignee = CASE WHEN assigned_to_user THEN 'user' ELSE assignee_agent_id END"
    )
    op.execute("DELETE FROM issue_activities WHERE kind IN ('claimed', 'released')")
    _keep_sequence(_drop_old_columns)


def _drop_old_columns() -> None:
    # 删列要重建表：带上 AUTOINCREMENT（issue 编号永不复用）
    with op.batch_alter_table("issues", schema=None, table_kwargs={"sqlite_autoincrement": True}) as batch_op:
        batch_op.drop_index(batch_op.f("ix_issues_assignee_agent_id"))
        batch_op.create_index(batch_op.f("ix_issues_assignee"), ["assignee"], unique=False)
        batch_op.drop_column("sessions")
        batch_op.drop_column("assigned_to_user")
        batch_op.drop_column("assignee_agent_id")


def downgrade() -> None:
    with op.batch_alter_table("issues", schema=None) as batch_op:
        batch_op.add_column(sa.Column("assignee_agent_id", sa.VARCHAR(length=32), nullable=True))
        batch_op.add_column(
            sa.Column("assigned_to_user", sa.BOOLEAN(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(sa.Column("sessions", sqlite.JSON(), nullable=False, server_default="{}"))
    op.execute(
        "UPDATE issues SET assigned_to_user = (assignee IS 'user'), "
        "assignee_agent_id = CASE WHEN assignee = 'user' THEN NULL ELSE assignee END"
    )
    _keep_sequence(_drop_new_column)


def _drop_new_column() -> None:
    with op.batch_alter_table("issues", schema=None, table_kwargs={"sqlite_autoincrement": True}) as batch_op:
        batch_op.drop_index(batch_op.f("ix_issues_assignee"))
        batch_op.create_index(batch_op.f("ix_issues_assignee_agent_id"), ["assignee_agent_id"], unique=False)
        batch_op.drop_column("assignee")
