"""#661：删信件的幂等键（mail.dedupe_key 与唯一索引 uq_mail_dedupe_key）。交回结局与结局的记录在同一个事务里（#648），
每项占用只交回一次，不再按键去重。

用 SQLite 原生的 DROP COLUMN（3.35+），不走 batch 重建：mail 上有部分索引，重建要靠反射还原它们的 WHERE。

Revision ID: e7d4a1c9b062
Revises: c3f1a7d20e64
Create Date: 2026-11-12 11:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e7d4a1c9b062"
down_revision: str | None = "c3f1a7d20e64"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("uq_mail_dedupe_key", table_name="mail")
    op.execute("ALTER TABLE mail DROP COLUMN dedupe_key")


def downgrade() -> None:
    """列与索引恢复为空：旧代码只用它对之后的投信去重，已有信件没有键不影响。"""
    op.execute("ALTER TABLE mail ADD COLUMN dedupe_key VARCHAR(200)")
    op.create_index(
        "uq_mail_dedupe_key",
        "mail",
        ["session_id", "dedupe_key"],
        unique=True,
        sqlite_where=sa.text("dedupe_key IS NOT NULL"),
    )
