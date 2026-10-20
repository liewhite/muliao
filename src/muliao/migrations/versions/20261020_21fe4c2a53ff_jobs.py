"""后台任务：jobs 表（agent 在后台跑的 shell 命令，结束与运行中检查时给会话发事件，见 runtime.jobs）

Revision ID: 21fe4c2a53ff
Revises: 3e7de64a27e0
Create Date: 2026-10-20 13:25:38.820509
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "21fe4c2a53ff"
down_revision: str | None = "3e7de64a27e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=32), nullable=False),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("secrets", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("log_path", sa.String(length=255), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("reply_to", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_jobs_agent_id"), ["agent_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_jobs_session_id"), ["session_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_jobs_status"), ["status"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_jobs_status"))
        batch_op.drop_index(batch_op.f("ix_jobs_session_id"))
        batch_op.drop_index(batch_op.f("ix_jobs_agent_id"))

    op.drop_table("jobs")
