"""model_usage.session_id 加索引（#600）：会话页每次拉取都取这个会话的累计用量，没有索引是全表扫描。只加索引，不动数据；
降级删掉索引。

Revision ID: c0fe84d4f804
Revises: b5e2c8d47a16
Create Date: 2026-11-11 11:22:56.630304
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c0fe84d4f804"
down_revision: str | None = "b5e2c8d47a16"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_model_usage_session_id", "model_usage", ["session_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_model_usage_session_id", table_name="model_usage")
