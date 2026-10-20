"""通知：notifications 表；deliver task 的 payload 改为只引用通知

已有的 deliver task 各自转成一条通知（沿用 task 的 id 与创建时间），payload 改为 {"notification_id": ...}，
读取方不再需要区分新旧两种 payload。已投递或失败的视为已读，还没投递的保持未读，升级后角标不会突然堆满旧通知。

Revision ID: f9ce861f3c5f
Revises: 0e63aa5dab99
Create Date: 2026-10-20 10:13:37.671613
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f9ce861f3c5f"
down_revision: str | None = "0e63aa5dab99"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=True),
        sa.Column("unread", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("notifications", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_notifications_session_id"), ["session_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_notifications_unread"), ["unread"], unique=False)

    op.execute(
        "INSERT INTO notifications (id, title, body, session_id, unread, created_at) "
        "SELECT id, json_extract(payload, '$.title'), json_extract(payload, '$.body'), session_id, "
        "status IN ('pending', 'running'), created_at FROM tasks WHERE kind = 'deliver'"
    )
    op.execute("UPDATE tasks SET payload = json_object('notification_id', id) WHERE kind = 'deliver'")


def downgrade() -> None:
    op.execute(
        "UPDATE tasks SET payload = (SELECT json_object('title', n.title, 'body', n.body, 'session_id', n.session_id) "
        "FROM notifications n WHERE n.id = json_extract(tasks.payload, '$.notification_id')) WHERE kind = 'deliver'"
    )
    with op.batch_alter_table("notifications", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_notifications_unread"))
        batch_op.drop_index(batch_op.f("ix_notifications_session_id"))

    op.drop_table("notifications")
