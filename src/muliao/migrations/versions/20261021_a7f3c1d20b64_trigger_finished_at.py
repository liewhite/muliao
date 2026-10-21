"""trigger finished_at

触发器新增 finished_at（归档时间，已有数据为 NULL），名称的唯一性从整张表改为只在未归档的触发器之间
（部分唯一索引 WHERE finished_at IS NULL）：一次性触发器跑完归档之后，同名的可以重新创建。

SQLite 不能直接去掉建表时的 UNIQUE(name)，用 batch 模式重建这张表：copy_from 给出重建后的结构（不含
那条唯一约束），数据按列拷贝过去。recreate="always" 是必须的——只加一列的话 batch 默认走 ALTER TABLE，
那条唯一约束会原样留下。

Revision ID: a7f3c1d20b64
Revises: cd85203e73a8
Create Date: 2026-10-21 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7f3c1d20b64"
down_revision: str | None = "cd85203e73a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAME_UNFINISHED = "uq_triggers_name_unfinished"
_UNFINISHED = sa.text("finished_at IS NULL")


def _triggers(*, unique_name: bool, finished: bool) -> sa.Table:
    """重建前这张表的结构（batch 模式的 copy_from）：数据按这里的列拷贝，新表也按这里的约束建。

    upgrade 传 unique_name=False——列与现有表一致，只是重建后不再带那条整表唯一的 UNIQUE(name)。
    """
    return sa.Table(
        "triggers",
        sa.MetaData(),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("agent_id", sa.String(length=32), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_fired_at", sa.DateTime(), nullable=True),
        sa.Column("last_session_id", sa.String(length=36), nullable=True),
        sa.Column("precheck", sa.JSON(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.Column("precheck_output", sa.Text(), nullable=True),
        sa.Column("disabled_reason", sa.Text(), nullable=True),
        sa.Column("precheck_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        *([sa.Column("finished_at", sa.DateTime(), nullable=True)] if finished else []),
        sa.Index("ix_triggers_agent_id", "agent_id"),
        sa.PrimaryKeyConstraint("id"),
        *([sa.UniqueConstraint("name")] if unique_name else []),
        sqlite_autoincrement=True,
    )


def upgrade() -> None:
    copy_from = _triggers(unique_name=False, finished=False)
    with op.batch_alter_table("triggers", copy_from=copy_from, recreate="always", schema=None) as batch_op:
        batch_op.add_column(sa.Column("finished_at", sa.DateTime(), nullable=True))
    op.create_index(_NAME_UNFINISHED, "triggers", ["name"], unique=True, sqlite_where=_UNFINISHED)


def downgrade() -> None:
    op.drop_index(_NAME_UNFINISHED, table_name="triggers", sqlite_where=_UNFINISHED)
    copy_from = _triggers(unique_name=True, finished=True)
    with op.batch_alter_table("triggers", copy_from=copy_from, recreate="always", schema=None) as batch_op:
        batch_op.drop_column("finished_at")
