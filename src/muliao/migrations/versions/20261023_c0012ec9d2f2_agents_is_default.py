"""agents.is_default：系统默认 agent（Web 新对话默认选中），部分唯一索引保证最多一个。

只加列和索引，不动已有数据（全部为 0，即没有默认 agent）。降级删索引和列，用 SQLite 原生 ALTER TABLE，不重建 agents 表。

Revision ID: c0012ec9d2f2
Revises: 75eeddddf94a
Create Date: 2026-10-23 01:00:02.437766
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c0012ec9d2f2"
down_revision: str | None = "75eeddddf94a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agents", sa.Column("is_default", sa.Boolean(), server_default=sa.text("0"), nullable=False)
    )
    op.create_index(
        "uq_agents_default", "agents", ["is_default"], unique=True, sqlite_where=sa.text("is_default")
    )


def downgrade() -> None:
    op.drop_index("uq_agents_default", table_name="agents")
    op.drop_column("agents", "is_default")
