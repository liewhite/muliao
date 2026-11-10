"""会话删 kind 列（#581）：「是不是 thread」只由来源与父会话定（委派开出、origin 为 delegation），不再另存一份种类。
只删一列不动别的数据；降级按 origin 重算回 kind（delegation 为 thread，其余为 conversation）。

Revision ID: a7d3e5c91b04
Revises: ff49bc305899
Create Date: 2026-11-11 00:50:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7d3e5c91b04"
down_revision: str | None = "ff49bc305899"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 列上没有索引与约束，SQLite 原生 DROP COLUMN，不重建表
    op.drop_column("sessions", "kind")


def downgrade() -> None:
    op.add_column(
        "sessions", sa.Column("kind", sa.String(length=16), nullable=False, server_default="conversation")
    )
    op.execute("UPDATE sessions SET kind = 'thread' WHERE origin = 'delegation'")
