"""触发器加 route_error：事件内容里写错而被丢掉的 `@route` 地址与原因（#569）。只加一列可空列，不动数据。

Revision ID: ff49bc305899
Revises: c4a81f6e9b23
Create Date: 2026-11-10 22:24:45.654068
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ff49bc305899"
down_revision: str | None = "c4a81f6e9b23"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("triggers", sa.Column("route_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("triggers", "route_error")
