"""drop sessions.unread

会话的未读改为从通知派生（#175）：有指向它的未读通知就是未读，删除 sessions.unread 列。会删数据：原来的未读标记
（每次运行产生消息就标，大多是没人需要看的定时任务）不保留；升级后的未读只看 notifications。

降级加回这一列（server_default 0），并按当时的通知回填：有指向它的未读通知的会话为未读。

Revision ID: 820a32ee48fd
Revises: bd1e7f7a8324
Create Date: 2026-10-23 16:37:59.597861
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "820a32ee48fd"
down_revision: str | None = "bd1e7f7a8324"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.drop_column("unread")


def downgrade() -> None:
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("unread", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.execute(
        "UPDATE sessions SET unread = 1 WHERE id IN "
        "(SELECT session_id FROM notifications WHERE unread = 1 AND session_id IS NOT NULL)"
    )
