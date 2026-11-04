"""trigger route

触发器新增 route（事件的默认投递目标，routing.api.Target 的 JSON）；已有数据为 NULL，即新开它的 agent 的会话，
行为不变。不动数据。增删列都用 SQLite 原生 ALTER TABLE（列上没有索引与约束），不重建表，AUTOINCREMENT、
sqlite_sequence 与名称的部分唯一索引不受影响。降级同时删掉 issue 时间线上的 event 动态（旧代码不认识这个取值，
读到会报错）。

Revision ID: a35e2d010dbf
Revises: 8f956292ff5b
Create Date: 2026-11-04 23:17:58.725741
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a35e2d010dbf"
down_revision: str | None = "8f956292ff5b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("triggers", sa.Column("route", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.execute("DELETE FROM issue_activities WHERE kind = 'event'")
    op.drop_column("triggers", "route")
