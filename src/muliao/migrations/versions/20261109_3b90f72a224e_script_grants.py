"""script_grants（#531）：用户对 skill 脚本调用的「始终允许」（agent、skill、脚本、参数、secret、skill 内容指纹、来历、命中
次数）。新表，不动已有数据。

降级：删表（旧代码没有始终允许，同样的调用重新要批准）。

Revision ID: 3b90f72a224e
Revises: b7e3a9c41d02
Create Date: 2026-11-09 23:57:01.390442
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3b90f72a224e"
down_revision: str | None = "b7e3a9c41d02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:

    op.create_table(
        "script_grants",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("agent_id", sa.String(length=32), nullable=False),
        sa.Column("skill", sa.String(length=100), nullable=False),
        sa.Column("script", sa.Text(), nullable=False),
        sa.Column("args", sa.JSON(), nullable=False),
        sa.Column("secrets", sa.JSON(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("call_id", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("script_grants", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_script_grants_agent_id"), ["agent_id"], unique=False)


def downgrade() -> None:

    with op.batch_alter_table("script_grants", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_script_grants_agent_id"))

    op.drop_table("script_grants")
