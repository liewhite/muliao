"""add issue dependencies

新表 issue_dependencies：issue 之间的依赖（有向无环图），任一端删除时级联删除。不动已有数据。
时间线新增两种动态（depends_on、dependency），存为字符串，升级不用改表；降级时删掉这两种动态
（旧版本读到未知取值会报错）。

Revision ID: f462ce534d7c
Revises: 6bba488992eb
Create Date: 2026-10-21 19:55:55.031207
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f462ce534d7c"
down_revision: str | None = "6bba488992eb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "issue_dependencies",
        sa.Column("issue_id", sa.Integer(), nullable=False),
        sa.Column("depends_on_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["depends_on_id"], ["issues.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["issue_id"], ["issues.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("issue_id", "depends_on_id"),
    )
    with op.batch_alter_table("issue_dependencies", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_issue_dependencies_depends_on_id"), ["depends_on_id"], unique=False
        )


def downgrade() -> None:
    op.execute("DELETE FROM issue_activities WHERE kind IN ('depends_on', 'dependency')")
    with op.batch_alter_table("issue_dependencies", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_issue_dependencies_depends_on_id"))
    op.drop_table("issue_dependencies")
