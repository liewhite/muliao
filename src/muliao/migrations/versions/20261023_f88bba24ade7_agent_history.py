"""agent_history：agent 做过的事（只追加），加 FTS5 trigram 全文索引与禁止改删的触发器。

只新建表、虚表与触发器，不动已有数据。触发器：插入时同步到 agent_history_fts（refs 取 JSON 解码后的值、空格连接，不索引 JSON 原文）；UPDATE / DELETE 直接 RAISE(ABORT)。
以后若要用 batch 模式重建 agent_history，重建会丢掉这些触发器，必须在同一个迁移里重新建出。
降级删触发器、虚表和表（历史随之丢失）。

Revision ID: f88bba24ade7
Revises: c0012ec9d2f2
Create Date: 2026-10-23 11:22:21.389114
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f88bba24ade7"
down_revision: str | None = "c0012ec9d2f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_history",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("agent_id", sa.String(length=32), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("refs", sa.JSON(), nullable=False),
        sa.Column("importance", sa.SmallInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("agent_history", schema=None) as batch_op:
        batch_op.create_index("ix_agent_history_agent_day", ["agent_id", "day"], unique=False)
    op.execute("CREATE VIRTUAL TABLE agent_history_fts USING fts5(title, summary, refs, tokenize='trigram')")
    op.execute(
        "CREATE TRIGGER agent_history_fts_insert AFTER INSERT ON agent_history BEGIN "
        "INSERT INTO agent_history_fts(rowid, title, summary, refs) VALUES (new.id, new.title, new.summary, "
        "(SELECT group_concat(value, ' ') FROM json_each(new.refs))); "
        "END"
    )
    op.execute(
        "CREATE TRIGGER agent_history_no_update BEFORE UPDATE ON agent_history BEGIN "
        "SELECT RAISE(ABORT, 'agent_history is append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER agent_history_no_delete BEFORE DELETE ON agent_history BEGIN "
        "SELECT RAISE(ABORT, 'agent_history is append-only'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER agent_history_no_delete")
    op.execute("DROP TRIGGER agent_history_no_update")
    op.execute("DROP TRIGGER agent_history_fts_insert")
    op.execute("DROP TABLE agent_history_fts")
    with op.batch_alter_table("agent_history", schema=None) as batch_op:
        batch_op.drop_index("ix_agent_history_agent_day")
    op.drop_table("agent_history")
