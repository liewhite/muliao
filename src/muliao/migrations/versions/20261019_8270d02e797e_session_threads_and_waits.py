"""会话领域：子会话 thread 与等待关系

- sessions 增加 parent_session_id、thread、private。已有委派会话的父会话原先记在 origin_ref，迁到
  parent_session_id（父会话已不存在则置空），thread 名用子会话 id（保证同一父会话内唯一），private 为 false
  （此前的委派会话都对用户可见）。origin_ref 从此只记 cron job id。
- turn task 载荷增加 reply_to：此前是否推送由会话来源决定（cron 不推送，其余推送），按同一规则补齐。
- 新建 waits 表。

Revision ID: 8270d02e797e
Revises: 8b4e2f6a1c37
Create Date: 2026-10-19 17:10:34.863133
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import muliao.db

revision: str = "8270d02e797e"
down_revision: str | None = "8b4e2f6a1c37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "waits",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("waiter_task_id", sa.Integer(), nullable=False),
        sa.Column("tool_call_id", sa.String(length=100), nullable=False),
        sa.Column("target_task_id", sa.Integer(), nullable=False),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", muliao.db.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["waiter_task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("waits", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_waits_target_task_id"), ["target_task_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_waits_waiter_task_id"), ["waiter_task_id"], unique=False)

    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("parent_session_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("thread", sa.String(length=100), nullable=True))
        # 已有行需要一个值才能加非空列；随后去掉服务端默认值，与模型一致（由应用显式写入）
        batch_op.add_column(sa.Column("private", sa.Boolean(), nullable=False, server_default=sa.false()))

    op.execute(
        "UPDATE sessions SET "
        "parent_session_id = (SELECT p.id FROM sessions p WHERE p.id = sessions.origin_ref), "
        "thread = id, origin_ref = NULL "
        "WHERE origin = 'delegation'"
    )
    op.execute(
        "UPDATE tasks SET payload = json_set(payload, '$.reply_to', "
        "CASE (SELECT origin FROM sessions s WHERE s.id = tasks.session_id) WHEN 'cron' THEN 'none' ELSE 'user' END) "
        "WHERE kind = 'turn'"
    )

    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.alter_column("private", existing_type=sa.Boolean(), server_default=None)
        batch_op.create_index(
            batch_op.f("ix_sessions_parent_session_id"), ["parent_session_id"], unique=False
        )
        batch_op.create_index(
            "uq_sessions_thread",
            ["parent_session_id", "thread"],
            unique=True,
            sqlite_where=sa.text("parent_session_id IS NOT NULL"),
        )
        batch_op.create_foreign_key(
            "fk_sessions_parent_session_id", "sessions", ["parent_session_id"], ["id"], ondelete="SET NULL"
        )


def downgrade() -> None:
    raise NotImplementedError("session threads migration is irreversible")
