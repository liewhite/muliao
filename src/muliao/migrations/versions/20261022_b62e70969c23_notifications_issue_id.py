"""notifications issue_id

notifications 加可空列 issue_id（交给用户审核的 issue，点开通知打开它）与索引；已有通知为 NULL，不动数据。
降级删掉这一列（重建表，带上 AUTOINCREMENT 并保住 sqlite_sequence：通知 id 是已读游标，不能复用）。

Revision ID: b62e70969c23
Revises: b9716fea0122
Create Date: 2026-10-22 14:18:14.684110
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from muliao.platform.migration_ops import keep_sequence

revision: str = "b62e70969c23"
down_revision: str | None = "b9716fea0122"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("notifications", schema=None) as batch_op:
        batch_op.add_column(sa.Column("issue_id", sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f("ix_notifications_issue_id"), ["issue_id"], unique=False)


def downgrade() -> None:
    with (
        keep_sequence("notifications"),
        op.batch_alter_table(
            "notifications", schema=None, table_kwargs={"sqlite_autoincrement": True}
        ) as batch_op,
    ):
        batch_op.drop_index(batch_op.f("ix_notifications_issue_id"))
        batch_op.drop_column("issue_id")
