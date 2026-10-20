"""会话事件队列：session_events 表（后台任务完成、运行中的检查等，等下一轮一起处理，见 runtime/events）

Revision ID: 3e7de64a27e0
Revises: f9ce861f3c5f
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3e7de64a27e0"
down_revision: str | None = "f9ce861f3c5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "session_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("source", sa.String(length=100), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("reply_to", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_by", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["consumed_by"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("session_events", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_session_events_session_id"), ["session_id"], unique=False)
        batch_op.create_index(
            "ix_session_events_queued",
            ["session_id"],
            unique=False,
            sqlite_where=sa.text("consumed_by IS NULL"),
        )


def downgrade() -> None:
    with op.batch_alter_table("session_events", schema=None) as batch_op:
        batch_op.drop_index("ix_session_events_queued")
        batch_op.drop_index(batch_op.f("ix_session_events_session_id"))
    op.drop_table("session_events")
