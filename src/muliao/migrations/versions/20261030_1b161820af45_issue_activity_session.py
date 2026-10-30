"""issue activity session

issue_activities 加可空列 session_id（这条动态发生在哪个会话，界面可跳转，#310）与索引；已有动态为 NULL，不动数据。
降级删掉这一列（重建表，带上 AUTOINCREMENT 并保住 sqlite_sequence）。

Revision ID: 1b161820af45
Revises: d5ac95c1847f
Create Date: 2026-10-30 10:57:06.579366
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from muliao.platform.migration_ops import keep_sequence

revision: str = "1b161820af45"
down_revision: str | None = "d5ac95c1847f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("issue_activities", schema=None) as batch_op:
        batch_op.add_column(sa.Column("session_id", sa.String(length=36), nullable=True))
        batch_op.create_index(batch_op.f("ix_issue_activities_session_id"), ["session_id"], unique=False)


def downgrade() -> None:
    with (
        keep_sequence("issue_activities"),
        op.batch_alter_table(
            "issue_activities", schema=None, table_kwargs={"sqlite_autoincrement": True}
        ) as batch_op,
    ):
        batch_op.drop_index(batch_op.f("ix_issue_activities_session_id"))
        batch_op.drop_column("session_id")
