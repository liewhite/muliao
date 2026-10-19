"""secret 分公共与 agent 私有

主键由 key 改为自增 id，新增 owner（None 为公共）；同一作用域内 key 唯一。
现有 secret 全部成为公共 secret：此前所有 agent 都能使用它们，迁移后可见范围不变。

不可降级：降级后同名的私有与公共 secret 无法共存。

Revision ID: 8e72658bd3ca
Revises: f0076301ba19
Create Date: 2026-10-19 18:13:50.418343
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import muliao.db

revision: str = "8e72658bd3ca"
down_revision: str | None = "f0076301ba19"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 主键变化，SQLite 只能重建表
    op.create_table(
        "secrets_new",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("owner", sa.String(length=32), nullable=True),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("updated_at", muliao.db.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["owner"], ["agents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        "INSERT INTO secrets_new (owner, key, ciphertext, description, updated_at) "
        "SELECT NULL, key, ciphertext, description, updated_at FROM secrets ORDER BY key"
    )
    op.drop_table("secrets")
    op.rename_table("secrets_new", "secrets")
    with op.batch_alter_table("secrets") as batch_op:
        batch_op.create_index(
            "uq_secrets_private", ["owner", "key"], unique=True, sqlite_where=sa.text("owner IS NOT NULL")
        )
        batch_op.create_index(
            "uq_secrets_public", ["key"], unique=True, sqlite_where=sa.text("owner IS NULL")
        )


def downgrade() -> None:
    raise NotImplementedError("private and public secrets with the same key cannot be merged back")
