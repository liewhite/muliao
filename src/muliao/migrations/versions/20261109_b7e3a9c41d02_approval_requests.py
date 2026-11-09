"""approval_requests（#530）：哪些工具调用停下来等用户批准了（会话 + 调用 id）。运行停在等批准的调用上时在收尾事务里
写入，是「没有结果的调用在等批准」与「执行到一半被打断」的区分依据；taken_at 记批准第一次交给运行去执行的时间，
防止重复执行。新表，不动已有数据。

降级：删表（旧代码不认识等批准的调用；降级前应没有停在等批准上的会话，否则它们按「被中断」补结果）。

Revision ID: b7e3a9c41d02
Revises: a35e2d010dbf
Create Date: 2026-11-09 20:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7e3a9c41d02"
down_revision: str | None = "a35e2d010dbf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "approval_requests",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("call_id", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("taken_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_approval_requests_call", "approval_requests", ["session_id", "call_id"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_approval_requests_call", table_name="approval_requests")
    op.drop_table("approval_requests")
