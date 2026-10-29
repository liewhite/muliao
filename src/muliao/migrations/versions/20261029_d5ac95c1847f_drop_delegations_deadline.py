"""drop delegations deadline

ask_agent 不再有 timeout（#307：agent 的运行本身有界），删掉 delegations.deadline（ask_agent 的超时时刻）。
进行中的 ask_agent 委派丢掉超时、照常等结局；不动别的数据。降级加回可空列，旧委派都没有 deadline。

Revision ID: d5ac95c1847f
Revises: 9e3b1c7a5d20
Create Date: 2026-10-29 17:26:14.157876
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5ac95c1847f"
down_revision: str | None = "9e3b1c7a5d20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 列上没有索引与约束，SQLite 原生 DROP COLUMN，不重建表
    op.drop_column("delegations", "deadline")


def downgrade() -> None:
    op.add_column("delegations", sa.Column("deadline", sa.DateTime(), nullable=True))
