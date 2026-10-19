"""触发器的前置检查（precheck）、最近一次检查的时间与输出

已有触发器没有前置检查（NULL：每次都交给 agent），行为不变。

Revision ID: 0e63aa5dab99
Revises: b7c41e2d9a10
Create Date: 2026-10-20 00:14:13.988216
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0e63aa5dab99"
down_revision: str | None = "b7c41e2d9a10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("triggers", schema=None) as batch_op:
        batch_op.add_column(sa.Column("precheck", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("last_checked_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("precheck_output", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("triggers", schema=None) as batch_op:
        batch_op.drop_column("precheck_output")
        batch_op.drop_column("last_checked_at")
        batch_op.drop_column("precheck")
