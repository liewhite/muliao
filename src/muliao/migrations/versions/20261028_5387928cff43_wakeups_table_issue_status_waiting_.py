"""到点唤醒表 wakeups（wake_me）；issue 状态去掉 waiting（#261）

- 新表 wakeups：会话约的到点唤醒，在会话上的占用 holder 为 wake:<会话>:<调用 id>。
- issues.status：waiting 改成 in_progress（在等什么由会话自己的等待看出：后台任务、委派、到点唤醒；在等上游由依赖
  派生）。时间线里旧的 status 动态（change 里的 "waiting"）是历史，不改。

降级不可逆的部分：in_progress 保持不变（分不出原来是不是 waiting）。删表之前把还在等的唤醒的占用记为 cleared：旧代码
没有到点投信的人，它们会让会话一直算忙（waiting、不能删除）。

Revision ID: 5387928cff43
Revises: d0f01dc4c372
Create Date: 2026-10-28 09:11:45.212652
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import muliao.platform.db

revision: str = "5387928cff43"
down_revision: str | None = "d0f01dc4c372"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "wakeups",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("holder", sa.String(length=200), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("due_at", muliao.platform.db.UTCDateTime(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_at", muliao.platform.db.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("wakeups", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_wakeups_session_id"), ["session_id"], unique=False)
        batch_op.create_index("uq_wakeups_holder", ["holder"], unique=True)
    op.execute("UPDATE issues SET status = 'in_progress' WHERE status = 'waiting'")


def downgrade() -> None:
    op.execute(
        "UPDATE session_holds SET status = 'cleared', ended_at = datetime('now') "
        "WHERE holder LIKE 'wake:%' AND status = 'active'"
    )
    with op.batch_alter_table("wakeups", schema=None) as batch_op:
        batch_op.drop_index("uq_wakeups_holder")
        batch_op.drop_index(batch_op.f("ix_wakeups_session_id"))
    op.drop_table("wakeups")
