"""对话里的文件（files 表）；turn task 的 payload 增加 attachments

已有的 turn task 写入时还没有附件，补上空列表，读取方不再需要区分新旧两种 payload。

Revision ID: 9dda44ebb470
Revises: 4a351a020890
Create Date: 2026-10-19 22:33:26.053051
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9dda44ebb470"
down_revision: str | None = "4a351a020890"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "files",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        "UPDATE tasks SET payload = json_set(payload, '$.attachments', json('[]')) WHERE kind = 'turn'"
    )


def downgrade() -> None:
    op.execute("UPDATE tasks SET payload = json_remove(payload, '$.attachments') WHERE kind = 'turn'")
    op.drop_table("files")
