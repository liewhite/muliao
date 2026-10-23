"""agents.thinking：agent 的思考档位（agents.service.Thinking）。

只加列，已有 agent 取 server_default "default"（不传 thinking，与之前的行为一致），不动其他数据。降级删列，用 SQLite
原生 ALTER TABLE，不重建 agents 表。

Revision ID: bd1e7f7a8324
Revises: f88bba24ade7
Create Date: 2026-10-23 15:58:44.144143
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bd1e7f7a8324"
down_revision: str | None = "f88bba24ade7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agents", sa.Column("thinking", sa.String(length=16), server_default="default", nullable=False)
    )


def downgrade() -> None:
    op.drop_column("agents", "thinking")
