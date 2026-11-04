"""sessions 加可空列 compaction_threshold_tokens：建会话时给的压缩阈值，NULL 用全局默认（#372）。

不动数据：已有会话都是 NULL，行为不变。加/删一个无索引、无约束的可空列，用 SQLite 原生 ALTER TABLE，不重建表。

Revision ID: 8f956292ff5b
Revises: cb9354fa36e7
Create Date: 2026-11-04 19:44:51.665051
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8f956292ff5b"
down_revision: str | None = "cb9354fa36e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("compaction_threshold_tokens", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "compaction_threshold_tokens")
