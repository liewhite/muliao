"""agent group and archived_at

agents 加两个可空列：group（分组名）、archived_at（归档时间，非空即已归档）。不动现有数据：升级后所有 agent
不分组、未归档。降级删掉这两列（分组与归档状态丢失，归档的 agent 回到可用）。

Revision ID: 75cc40ae1d3d
Revises: aa38462ddc18
Create Date: 2026-10-26 15:05:43.024556
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "75cc40ae1d3d"
down_revision: str | None = "aa38462ddc18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("group", sa.String(length=64), nullable=True))
    op.add_column("agents", sa.Column("archived_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("agents", "archived_at")
    op.drop_column("agents", "group")
